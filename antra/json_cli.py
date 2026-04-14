import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

# On Windows, Python's subprocess module defaults to the system locale encoding
# (usually cp1252) for text-mode pipes. yt-dlp spawns ffmpeg internally and
# reads its stderr in text mode without specifying encoding, which causes
# UnicodeDecodeError when ffmpeg outputs UTF-8 content (e.g. Unicode filenames
# or progress bars). Patch subprocess.Popen to default to UTF-8 + replace on
# Windows so all child process pipe I/O is handled gracefully.
if sys.platform == 'win32':
    import subprocess as _sp
    _orig_popen_init = _sp.Popen.__init__
    def _popen_utf8_default(self, *args, **kwargs):
        if (kwargs.get('text') or kwargs.get('universal_newlines')) and 'encoding' not in kwargs:
            kwargs['encoding'] = 'utf-8'
            kwargs.setdefault('errors', 'replace')
        _orig_popen_init(self, *args, **kwargs)
    _sp.Popen.__init__ = _popen_utf8_default

from antra.core.models import BulkDownloadProgress
from antra.core.service import AntraService, RuntimeOptions
from antra.core.events import EngineEvent
from antra.utils.runtime import ensure_runtime_environment


# ── Mutagen probe fallback ────────────────────────────────────────────────────
# Used when ffprobe is not available (no system ffprobe and imageio_ffmpeg
# doesn't bundle ffprobe). Returns the same JSON shape as ffprobe so the
# frontend works identically on all machines.

def _extract_mutagen_tags(tags) -> dict:
    """Normalise tag objects from any mutagen format into a flat dict."""
    result = {}
    if tags is None:
        return result

    # VorbisComment (FLAC, OGG, Opus) and APEv2 both expose dict-style .get()
    if hasattr(tags, "get"):
        for field, candidates in [
            ("title",  ["title", "TITLE"]),
            ("artist", ["artist", "ARTIST"]),
            ("album",  ["album",  "ALBUM"]),
            ("tracknumber", ["tracknumber", "TRACKNUMBER"]),
            ("date",   ["date", "DATE", "year", "YEAR"]),
        ]:
            for k in candidates:
                v = tags.get(k)
                if v:
                    result[field] = v[0] if isinstance(v, list) else str(v)
                    break

    # ID3 (MP3) – tags.getall() returns list of frame objects
    if hasattr(tags, "getall"):
        for frame_id, field in [
            ("TIT2", "title"), ("TPE1", "artist"), ("TALB", "album"),
            ("TRCK", "tracknumber"), ("TDRC", "date"),
        ]:
            frames = tags.getall(frame_id)
            if frames:
                v = frames[0]
                result[field] = str(v.text[0]) if hasattr(v, "text") and v.text else str(v)

    # MP4/M4A – dict-like but with Apple four-char keys
    if not result and hasattr(tags, "items"):
        mp4_map = {
            "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
            "trkn": "tracknumber", "\xa9day": "date",
        }
        for k, field in mp4_map.items():
            v = tags.get(k)
            if v:
                val = v[0]
                result[field] = str(val[0]) if isinstance(val, tuple) else str(val)

    return result


def _probe_via_mutagen(file_path: str) -> dict:
    """Pure-Python probe via mutagen — used when ffprobe is unavailable."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return {"error": "ffprobe not available and mutagen not installed"}

    try:
        audio = MutagenFile(file_path)
    except Exception as exc:
        return {"error": f"mutagen: {exc}"}

    if audio is None:
        return {"error": "mutagen: unrecognised audio format"}

    info = audio.info
    file_size = os.path.getsize(file_path)
    duration  = float(getattr(info, "length", 0))
    channels  = int(getattr(info, "channels", 0))
    sample_rate = int(getattr(info, "sample_rate", 0))

    # Codec from mutagen class name
    type_name = type(audio).__name__
    _codec_map = {
        "FLAC": "flac", "MP3": "mp3", "OggVorbis": "vorbis",
        "OggOpus": "opus", "OggFLAC": "flac", "OggSpeex": "speex",
        "WAVE": "pcm_s16le", "AIFF": "pcm_s16be",
        "MonkeysAudio": "ape", "WavPack": "wavpack",
        "ASF": "wmav2", "MP4": "aac",
    }
    codec_name = _codec_map.get(type_name, type_name.lower())

    # MP4 can be AAC or ALAC — check info.codec if present
    if type_name == "MP4":
        codec_attr = getattr(info, "codec", "") or ""
        if codec_attr.lower().startswith("alac"):
            codec_name = "alac"

    # Bit depth (lossless formats expose bits_per_sample)
    bits_per_sample = getattr(info, "bits_per_sample", None)

    # Bit rate: mutagen.info.bitrate is always in bps (bits per second)
    mutagen_bitrate = getattr(info, "bitrate", None)
    if mutagen_bitrate and mutagen_bitrate > 0:
        bit_rate_str = str(int(mutagen_bitrate))
    elif duration > 0:
        bit_rate_str = str(int(file_size * 8 / duration))
    else:
        bit_rate_str = "0"

    tags = _extract_mutagen_tags(audio.tags)

    stream: dict = {
        "codec_name":     codec_name,
        "codec_type":     "audio",
        "sample_rate":    str(sample_rate),
        "channels":       channels,
        "channel_layout": "stereo" if channels == 2 else "mono" if channels == 1 else str(channels),
    }
    if bits_per_sample:
        stream["bits_per_raw_sample"] = str(bits_per_sample)

    return {
        "streams": [stream],
        "format": {
            "filename": file_path,
            "duration": str(duration),
            "bit_rate": bit_rate_str,
            "size":     str(file_size),
            "tags":     tags,
        },
    }


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
            "quality_label": event.quality_label,
            "attempt": event.attempt,
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
    parser.add_argument("--get-ffmpeg-dir", action="store_true",
                        help="Print the paths to bundled ffmpeg/ffprobe (two lines) and exit")
    parser.add_argument("--probe", metavar="FILE",
                        help="Run ffprobe on FILE and print JSON metadata, then exit")
    parser.add_argument("--spectrogram", metavar="FILE",
                        help="Generate spectrogram PNG for FILE and print base64 JSON, then exit")
    parser.add_argument("--discography", metavar="ARTIST_URL",
                        help="Fetch artist discography info as JSON and exit")
    parser.add_argument("--search-artists", metavar="QUERY",
                        help="Search for artists by name and return scored results as JSON, then exit")
    parser.add_argument("--search-source", default="spotify", choices=["spotify", "apple"],
                        help="Source to use for artist search (default: spotify)")
    args = parser.parse_args()

    ensure_runtime_environment()

    if args.discography:
        url = args.discography
        try:
            if "music.apple.com" in url and "/artist/" in url:
                from antra.core.apple_fetcher import AppleFetcher
                fetcher = AppleFetcher()
                info = fetcher.fetch_artist_discography_info(url)
            elif "music.amazon.com" in url and "/artists/" in url:
                from antra.core.config import load_config
                from antra.core.amazon_music_fetcher import AmazonMusicFetcher
                cfg = load_config()
                fetcher = AmazonMusicFetcher(mirrors=cfg.amazon_mirrors)
                info = fetcher.fetch_artist_discography_info(url)
            else:
                from antra.core.config import load_config
                from antra.core.spotify import SpotifyClient
                cfg = load_config()
                sp = SpotifyClient(
                    cfg.spotify_client_id,
                    cfg.spotify_client_secret,
                    cfg.spotify_market,
                    redirect_uri=cfg.spotify_redirect_uri,
                    auth_storage_path=cfg.spotify_auth_path,
                )
                info = sp.fetch_artist_discography_info(url)
            print(json.dumps({"type": "discography", "data": info}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.search_artists:
        try:
            from antra.core.config import load_config
            cfg = load_config()
            service = AntraService(config=cfg)
            results = service.search_artists(args.search_artists, source=args.search_source)
            print(json.dumps({"type": "artist_search", "data": results}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.probe:
        from antra.utils.runtime import get_ffprobe_exe
        import subprocess
        ffprobe = get_ffprobe_exe()
        if ffprobe:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", "-select_streams", "a:0", args.probe],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                print(r.stdout.decode("utf-8", errors="replace"), flush=True)
            else:
                print(json.dumps({"error": r.stderr.decode("utf-8", errors="replace")}), flush=True)
        else:
            # ffprobe not available — fall back to mutagen (pure Python, always bundled)
            result = _probe_via_mutagen(args.probe)
            print(json.dumps(result), flush=True)
        sys.exit(0)

    if args.spectrogram:
        from antra.utils.runtime import get_ffmpeg_exe
        import subprocess, base64 as _b64, tempfile, os as _os
        ffmpeg = get_ffmpeg_exe()
        if not ffmpeg:
            print(json.dumps({"error": "ffmpeg not found"}), flush=True)
            sys.exit(1)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", args.spectrogram,
                 "-lavfi", "showspectrumpic=s=1200x300:mode=combined:legend=0:color=viridis:scale=log:gain=4",
                 "-frames:v", "1", tmp_path],
                capture_output=True, timeout=120,
            )
            if r.returncode == 0 and _os.path.exists(tmp_path):
                with open(tmp_path, "rb") as f:
                    data = _b64.b64encode(f.read()).decode()
                print(json.dumps({"data": data}), flush=True)
            else:
                print(json.dumps({"error": r.stderr.decode("utf-8", errors="replace")}), flush=True)
        finally:
            if _os.path.exists(tmp_path):
                _os.unlink(tmp_path)
        sys.exit(0)

    if args.get_ffmpeg_dir:
        from antra.utils.runtime import get_ffmpeg_exe, get_ffprobe_exe
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
        # Output two lines: ffmpeg full path, ffprobe full path (empty if not found)
        print(ffmpeg or "")
        print(ffprobe or "")
        sys.exit(0)
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

            if "soulseek_seed_after_download" in settings:
                os.environ["SOULSEEK_SEED_AFTER_DOWNLOAD"] = "true" if settings["soulseek_seed_after_download"] else "false"

            # sources_enabled: ["hifi", "soulseek"] controls which adapter groups are active.
            # Empty or absent = all enabled (auto).
            sources_enabled = settings.get("sources_enabled") or []
            if sources_enabled:
                os.environ["SOURCES_ENABLED"] = ",".join(sources_enabled)
            else:
                os.environ["SOURCES_ENABLED"] = ""

        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": f"Failed to load config: {e}"}))

    # Source preference is now driven by sources_enabled; keep auto as the resolver default.
    os.environ["SOURCE_PREFERENCE"] = "auto"

    from antra.core.config import load_config
    from antra.utils.organizer import LibraryOrganizer
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

    # Build the organizer once — it scans the entire library on init, which can
    # be very slow on a NAS. Reusing one instance across all URLs in this batch
    # means the scan happens exactly once per run, not once per URL.
    try:
        organizer = LibraryOrganizer(cfg.output_dir)
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error", "message": f"Cannot access output directory: {e}"}))
        print(json.dumps({"type": "done"}))
        return

    print(json.dumps({"type": "log", "level": "info", "message": f"Preparing library update for {len(playlists_to_run)} source(s)"}))

    import os as _os
    from datetime import datetime

    for url in playlists_to_run:
        _url_start = time.time()
        try:
            print(json.dumps({"type": "log", "level": "info", "message": f"Syncing playlist to library: {url}"}))
            tracks = service.fetch_playlist_tracks(url, options=options)
            results = service.download_tracks(
                tracks,
                options=options,
                event_callback=emit_event,
                organizer=organizer,
            )

            _elapsed = time.time() - _url_start
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
                "error": None,
                "sources": {},
                "date": datetime.now().isoformat(),
                "total_mb": round(_total_bytes / (1024 * 1024), 1),
                "elapsed_seconds": round(_elapsed),
            }

            for r in results:
                if r.status.name == "COMPLETED" and r.source_used:
                    summary["sources"][r.source_used] = summary["sources"].get(r.source_used, 0) + 1

            downloaded = summary["downloaded"]
            print(json.dumps({"type": "library_update", "tracks_added": downloaded, "url": url}), flush=True)
            print(json.dumps(summary), flush=True)

        except Exception as e:
            error_msg = str(e)
            print(json.dumps({"type": "log", "level": "error", "message": error_msg}))
            # Always emit a summary even on hard failure so the frontend can
            # record this URL in history and let the user retry it.
            _elapsed = time.time() - _url_start
            summary = {
                "type": "playlist_summary",
                "url": url,
                "total": 0,
                "downloaded": 0,
                "failed": 0,
                "skipped": 0,
                "error": error_msg,
                "sources": {},
                "date": datetime.now().isoformat(),
                "total_mb": 0,
                "elapsed_seconds": round(_elapsed),
            }
            print(json.dumps(summary), flush=True)

    # Send a hard termination message
    print(json.dumps({"type": "done"}))

if __name__ == "__main__":
    main()
