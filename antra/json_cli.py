import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

from antra.core.models import BulkDownloadProgress
from antra.core.service import AntraService, RuntimeOptions
from antra.core.events import EngineEvent
from antra.utils.runtime import ensure_runtime_environment

class JsonLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Send log as a json object
            data = {"type": "log", "level": record.levelname.lower(), "message": msg}
            print(json.dumps(data), flush=True)
        except Exception:
            self.handleError(record)

def setup_json_logging(level=logging.INFO):
    logger = logging.getLogger()
    logger.setLevel(level)
    # Remove existing handlers
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    # Add json handler
    handler = JsonLogHandler()
    logger.addHandler(handler)

def emit_event(event: EngineEvent):
    data = {
        "type": "event",
        "name": event.type.value,
        "payload": {
            "track": event.track.title if event.track else None,
            "artist": event.track.artist_string if event.track else None,
            "track_index": event.track_index,
            "track_total": event.track_total,
            "message": event.message,
            "source": event.source,
            "error": event.error,
        }
    }
    print(json.dumps(data), flush=True)

def emit_progress(progress: BulkDownloadProgress):
    data = {
        "type": "progress",
        "stage": progress.stage,
        "playlist": progress.playlist.name if progress.playlist else None,
        "playlist_index": progress.playlist_index,
        "playlist_total": progress.playlist_total,
        "tracks_completed": progress.tracks_completed,
        "tracks_total": progress.tracks_total,
        "message": progress.message,
    }
    print(json.dumps(data), flush=True)


def main():
    _start_time = time.time()
    parser = argparse.ArgumentParser(description="Antra JSON backend")
    parser.add_argument("playlists", nargs="*", help="Spotify URLs to download")
    parser.add_argument("--config", help="Path to config.json from Go launcher")
    args = parser.parse_args()

    ensure_runtime_environment()
    setup_json_logging()

    # If config file is provided, load environments to os.environ so load_config picks them up
    if args.config and os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                settings = json.load(f)
            # Map settings to env vars for antra config to pick up
            if "download_path" in settings:
                os.environ["OUTPUT_DIR"] = settings["download_path"]
            if "soulseek_enabled" in settings:
                os.environ["SLSKD_AUTO_BOOTSTRAP"] = "true" if settings["soulseek_enabled"] else "false"
                # Desktop app uses dynamic auto-bootstrap. Force clear static env keys to trigger it always.
                os.environ["SLSKD_BASE_URL"] = ""
                os.environ["SLSKD_API_KEY"] = ""

            if settings.get("soulseek_username"):
                os.environ["SOULSEEK_USERNAME"] = settings["soulseek_username"]
            if settings.get("soulseek_password"):
                os.environ["SOULSEEK_PASSWORD"] = settings["soulseek_password"]

            # Map other possible API keys if configured in the Go app
            if settings.get("spotify_client_id"):
                os.environ["SPOTIFY_CLIENT_ID"] = settings["spotify_client_id"]
            if settings.get("spotify_client_secret"):
                os.environ["SPOTIFY_CLIENT_SECRET"] = settings["spotify_client_secret"]

            if "output_format" in settings and settings["output_format"]:
                os.environ["OUTPUT_FORMAT"] = settings["output_format"]
        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": f"Failed to load config: {e}"}))

    # We want to force auto mode since the user is in standard flow
    os.environ["SOURCE_PREFERENCE"] = "auto"

    from antra.core.config import load_config
    cfg = load_config()

    service = AntraService(cfg)
    options = RuntimeOptions(
        output_dir=cfg.output_dir,
        source_preference="auto",
        output_format=cfg.output_format
    )

    if not args.playlists:
        print(json.dumps({"type": "log", "level": "error", "message": "No playlists provided"}))
        return

    playlists_to_run = args.playlists

    # To process them normally
    print(json.dumps({"type": "log", "level": "info", "message": f"Preparing library update for {len(playlists_to_run)} source(s)"}))

    # We will fetch tracks for all playlists and then download
    for url in playlists_to_run:
        try:
            print(json.dumps({"type": "log", "level": "info", "message": f"Syncing playlist to library: {url}"}))
            results = service.download_playlist(
                url,
                options=options,
                event_callback=emit_event
            )

            import os as _os
            from datetime import datetime
            _elapsed = time.time() - _start_time
            _total_bytes = sum(
                _os.path.getsize(r.file_path)
                for r in results
                if r.file_path and _os.path.exists(r.file_path)
            )
            summary = {
                "type": "playlist_summary",
                "url": url,
                "total": len(results),
                "downloaded": sum(1 for r in results if r.status.name == "COMPLETED"),
                "failed": sum(1 for r in results if r.status.name == "FAILED"),
                "skipped": sum(1 for r in results if r.status.name == "SKIPPED"),
                "sources": {},
                "date": datetime.now().isoformat(),
                "total_mb": round(_total_bytes / (1024 * 1024), 1),
                "elapsed_seconds": round(_elapsed),
            }

            for r in results:
                if r.status.name == "COMPLETED" and r.source_used:
                    summary["sources"][r.source_used] = summary["sources"].get(r.source_used, 0) + 1

            # Emit structured library update events
            downloaded = summary["downloaded"]
            print(json.dumps({"type": "library_update", "tracks_added": downloaded, "url": url}), flush=True)

            print(json.dumps(summary), flush=True)

        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": str(e)}))

    # Send a hard termination message
    print(json.dumps({"type": "done"}))

if __name__ == "__main__":
    main()
