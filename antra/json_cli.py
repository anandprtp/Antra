import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
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
from antra.core.models import TrackMetadata


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
    track_payload = None
    if event.track:
        track_payload = asdict(event.track)
        track_payload.pop("lyrics", None)
        track_payload.pop("synced_lyrics", None)

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
            "track_data": track_payload,
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


def _infer_playlist_content_type(url: str, tracks) -> str:
    """Infer a display label (ALBUM, PLAYLIST, SINGLE, TRACK) from the URL and track list."""
    u = url.lower()
    if '/playlist/' in u:
        return 'PLAYLIST'
    if '/track/' in u:
        return 'SINGLE'
    if tracks and getattr(tracks[0], 'playlist_name', None):
        return 'PLAYLIST'
    return 'ALBUM'


def _playlist_artists_string(tracks) -> str:
    """Format album-level artist names into a display string (e.g. 'Future & Metro Boomin')."""
    if not tracks:
        return ''
    t = tracks[0]
    
    if getattr(t, 'playlist_name', None) and getattr(t, 'playlist_owner', None):
        res = t.playlist_owner
        desc = getattr(t, 'playlist_description', None)
        if desc:
            res += f" · {desc}"
        return res

    artists = list(t.album_artists) if t.album_artists else list(t.artists)
    if not artists:
        return ''
    if len(artists) == 1:
        return artists[0]
    return ', '.join(artists[:-1]) + ' & ' + artists[-1]


def _format_track_release_date(tracks) -> str:
    """Format the release date of the first track as 'Apr 12 2024' or '2024'."""
    if not tracks:
        return ''
    t = tracks[0]
    date_str = getattr(t, 'release_date', None) or ''
    if date_str:
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(str(date_str)[:10], '%Y-%m-%d')
            months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            return f"{months[d.month - 1]} {d.day} {d.year}"
        except Exception:
            pass
    year = getattr(t, 'release_year', None)
    return str(year) if year else ''


def _track_from_payload(payload: dict) -> TrackMetadata:
    payload = dict(payload or {})
    payload["artists"] = list(payload.get("artists") or [])
    payload["audio_traits"] = list(payload.get("audio_traits") or [])
    payload["genres"] = list(payload.get("genres") or [])
    payload["album_artists"] = list(payload.get("album_artists") or [])
    return TrackMetadata(**payload)


def _emit(obj: dict):
    """Print a JSON event line to stdout (picked up by the Go process)."""
    print(json.dumps(obj), flush=True)


def _wrap_tidal_session_payload(payload: dict) -> dict:
    """
    Normalize a pasted TIDAL session blob into the shape expected by tidalapi's
    session file loader.
    """
    normalized = {}
    for key, value in payload.items():
        if isinstance(value, dict) and "data" in value:
            normalized[key] = value
        else:
            normalized[key] = {"data": value}
    if "is_pkce" not in normalized:
        normalized["is_pkce"] = {"data": True}
    return normalized


def _build_tidal_session_payload(cfg) -> dict:
    auth_mode = (getattr(cfg, "tidal_auth_mode", "") or "session_json").strip().lower()

    if auth_mode == "session_json":
        raw = (getattr(cfg, "tidal_session_json", "") or "").strip()
        if not raw:
            raise RuntimeError("No TIDAL session JSON provided.")
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"TIDAL session JSON is invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("TIDAL session JSON must be an object.")
        return _wrap_tidal_session_payload(parsed)

    access_token = (getattr(cfg, "tidal_access_token", "") or "").strip()
    refresh_token = (getattr(cfg, "tidal_refresh_token", "") or "").strip()
    if not access_token or not refresh_token:
        raise RuntimeError("Manual TIDAL auth requires both access token and refresh token.")

    return {
        "token_type": {"data": (getattr(cfg, "tidal_token_type", "") or "Bearer").strip() or "Bearer"},
        "session_id": {"data": (getattr(cfg, "tidal_session_id", "") or "").strip()},
        "access_token": {"data": access_token},
        "refresh_token": {"data": refresh_token},
        "is_pkce": {"data": True},
    }


def _validate_tidal_auth(cfg) -> dict:
    if not getattr(cfg, "tidal_enabled", False):
        return {"ok": False, "message": "TIDAL Premium is disabled in Settings."}

    try:
        import tidalapi
    except ImportError:
        return {"ok": False, "message": "tidalapi is not installed in this build."}

    try:
        payload = _build_tidal_session_payload(cfg)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    fd, session_path = tempfile.mkstemp(prefix="antra_tidal_", suffix=".json")
    os.close(fd)
    try:
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        import pathlib
        config = tidalapi.Config(quality=tidalapi.Quality.hi_res_lossless)
        session = tidalapi.Session(config)
        ok = session.login_session_file(pathlib.Path(session_path), do_pkce=True, fn_print=lambda _msg: None)
        if not ok:
            return {"ok": False, "message": "TIDAL authentication failed. Re-import a fresh session."}

        user = getattr(session, "user", None)
        display_name = None
        user_id = None
        country_code = getattr(user, "country_code", None) if user else None
        if user:
            first = (getattr(user, "first_name", None) or "").strip()
            last = (getattr(user, "last_name", None) or "").strip()
            full = " ".join(part for part in [first, last] if part).strip()
            display_name = full or getattr(user, "username", None) or None
            user_id = getattr(user, "id", None)

        return {
            "ok": True,
            "message": "TIDAL session is valid.",
            "display_name": display_name,
            "user_id": user_id,
            "country_code": country_code,
            "auth_mode": (getattr(cfg, "tidal_auth_mode", "") or "session_json"),
        }
    except Exception as exc:
        return {"ok": False, "message": f"TIDAL validation failed: {exc}"}
    finally:
        try:
            os.unlink(session_path)
        except OSError:
            pass


def _download_podcast_url(url: str, cfg, output_dir: str) -> dict:
    """
    Handle a Spotify podcast URL (episode or show).
    Emits playlist_loaded, per-episode events, and returns a playlist_summary dict.
    Uses the same event format as the music engine so the frontend needs no changes.
    """
    from datetime import datetime as _dt
    from antra.core.podcast import (
        SpotifyPodcastClient, PodcastDownloader, PodcastAlreadyExistsError,
    )

    sp_dc = getattr(cfg, "spotify_sp_dc", "") or ""
    if not sp_dc:
        raise RuntimeError(
            "Spotify sp_dc cookie is not configured. "
            "Open Antra Settings → Spotify Podcasts and paste your sp_dc cookie "
            "(DevTools → Application → Cookies → open.spotify.com → sp_dc)."
        )

    client     = SpotifyPodcastClient(sp_dc)
    downloader = PodcastDownloader(client, output_dir)

    # Fetch metadata
    if "/episode/" in url:
        episode   = client.fetch_episode(url)
        episodes  = [episode]
        content_t = "EPISODE"
    else:
        _, episodes = client.fetch_show(url)
        content_t   = "PODCAST"

    if not episodes:
        raise RuntimeError("No episodes found for the given URL")

    show_name   = episodes[0].show_name
    artwork_url = episodes[0].artwork_url or ""

    _emit({
        "type":          "playlist_loaded",
        "title":         show_name,
        "artwork_url":   artwork_url,
        "content_type":  content_t,
        "artists_string":"Podcast",
        "release_date":  "",
        "quality_badge": "OGG",
        "track_count":   len(episodes),
        "tracks": [
            {"artist": ep.show_name, "title": ep.title, "duration_ms": ep.duration_ms or 0}
            for ep in episodes
        ],
    })

    _start = time.time()
    downloaded = failed = skipped = 0
    total_bytes = 0

    for i, ep in enumerate(episodes):
        _emit({
            "type": "event",
            "name": "track_started",
            "payload": {
                "track": ep.title, "artist": ep.show_name,
                "track_index": i, "track_total": len(episodes),
                "message": None, "source": None, "error": None,
                "quality_label": None, "attempt": None, "track_data": None,
            },
        })
        _emit({
            "type": "event",
            "name": "track_download_attempt",
            "payload": {
                "track": ep.title, "artist": ep.show_name,
                "track_index": i, "track_total": len(episodes),
                "message": "Downloading from Spotify",
                "source": "spotify", "error": None,
                "quality_label": None, "attempt": 1, "track_data": None,
            },
        })

        try:
            path, qlabel = downloader.download_episode(ep)
            downloaded += 1
            if os.path.exists(path):
                total_bytes += os.path.getsize(path)
            _emit({
                "type": "event",
                "name": "track_completed",
                "payload": {
                    "track": ep.title, "artist": ep.show_name,
                    "track_index": i, "track_total": len(episodes),
                    "message": f"Added to library",
                    "source": "spotify", "error": None,
                    "quality_label": qlabel, "attempt": 1, "track_data": None,
                },
            })
        except PodcastAlreadyExistsError:
            skipped += 1
            _emit({
                "type": "event",
                "name": "track_skipped",
                "payload": {
                    "track": ep.title, "artist": ep.show_name,
                    "track_index": i, "track_total": len(episodes),
                    "message": "Already exists", "source": None, "error": None,
                    "quality_label": None, "attempt": 1, "track_data": None,
                },
            })
        except Exception as e:
            failed += 1
            _emit({
                "type": "event",
                "name": "track_failed",
                "payload": {
                    "track": ep.title, "artist": ep.show_name,
                    "track_index": i, "track_total": len(episodes),
                    "message": None, "source": None, "error": str(e),
                    "quality_label": None, "attempt": 1, "track_data": None,
                },
            })

    elapsed = round(time.time() - _start)
    return {
        "type":             "playlist_summary",
        "url":              url,
        "title":            show_name,
        "artwork_url":      artwork_url,
        "total":            len(episodes),
        "downloaded":       downloaded,
        "failed":           failed,
        "skipped":          skipped,
        "error":            None,
        "sources":          {"spotify": downloaded} if downloaded else {},
        "date":             _dt.now().isoformat(),
        "total_mb":         round(total_bytes / (1024 * 1024), 1),
        "elapsed_seconds":  elapsed,
    }


def _run_tidal_oauth_login(cfg, config_path: str | None) -> None:
    """
    Initiate TIDAL OAuth device-code login using tidalapi's login_oauth_simple.

    Streams JSON events to stdout so the Go frontend can display them:
      {"type": "tidal_oauth_url", "url": "...", "code": "..."}
      {"type": "tidal_oauth_waiting"}
      {"type": "tidal_oauth_success", "session_json": "{...}", "display_name": "..."}
      {"type": "tidal_oauth_error", "message": "..."}
    """
    try:
        import tidalapi
    except ImportError:
        print(json.dumps({"type": "tidal_oauth_error", "message": "tidalapi is not installed."}), flush=True)
        return

    try:
        tidal_config = tidalapi.Config(quality=tidalapi.Quality.hi_res_lossless)
        session = tidalapi.Session(tidal_config)

        url_emitted = False

        def _oauth_print(msg: str) -> None:
            """Intercept tidalapi's print callback to emit structured JSON events."""
            nonlocal url_emitted
            msg = (msg or "").strip()
            if not msg:
                return

            # tidalapi emits something like:
            # "Opening browser to https://link.tidal.com/XXXXX for you to login."
            # or just the URL directly, or "DEVICE CODE: XXXXX"
            import re

            url_match = re.search(r'https?://\S+', msg)
            code_match = re.search(r'(?:code|Code)[:\s]+([A-Z0-9]{5,})', msg)

            if url_match and not url_emitted:
                url = url_match.group(0).rstrip('.')
                code = code_match.group(1) if code_match else ""
                print(json.dumps({
                    "type": "tidal_oauth_url",
                    "url": url,
                    "code": code,
                    "message": msg,
                }), flush=True)
                url_emitted = True
            else:
                # Generic status message
                print(json.dumps({"type": "tidal_oauth_status", "message": msg}), flush=True)

        print(json.dumps({"type": "tidal_oauth_status", "message": "Starting TIDAL OAuth login..."}), flush=True)

        # This blocks until the user authorises in their browser.
        session.login_oauth_simple(fn_print=_oauth_print)

        if not session.check_login():
            print(json.dumps({"type": "tidal_oauth_error", "message": "TIDAL login was not completed. Please try again."}), flush=True)
            return

        # Build session payload in the format Antra's session_json field expects.
        session_payload = {
            "token_type": {"data": session.token_type or "Bearer"},
            "access_token": {"data": session.access_token or ""},
            "refresh_token": {"data": session.refresh_token or ""},
            "is_pkce": {"data": True},
        }
        if getattr(session, "session_id", None):
            session_payload["session_id"] = {"data": session.session_id}

        session_json_str = json.dumps(session_payload)

        # Get user display name for success message.
        display_name = None
        country_code = None
        user = getattr(session, "user", None)
        if user:
            first = (getattr(user, "first_name", None) or "").strip()
            last = (getattr(user, "last_name", None) or "").strip()
            full = " ".join(p for p in [first, last] if p).strip()
            display_name = full or getattr(user, "username", None)
            country_code = getattr(user, "country_code", None)

        # Persist to Antra config.json if path is known.
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    antra_cfg = json.load(f)
                antra_cfg["tidal_enabled"] = True
                antra_cfg["tidal_auth_mode"] = "session_json"
                antra_cfg["tidal_session_json"] = session_json_str
                # Clear manual token fields to avoid confusion.
                antra_cfg.pop("tidal_access_token", None)
                antra_cfg.pop("tidal_refresh_token", None)
                antra_cfg.pop("tidal_session_id", None)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(antra_cfg, f, indent=2)
            except Exception as save_err:
                # Non-fatal: emit a warning but still emit the session JSON so UI can paste it.
                print(json.dumps({"type": "tidal_oauth_status", "message": f"Warning: could not save config automatically: {save_err}"}), flush=True)

        print(json.dumps({
            "type": "tidal_oauth_success",
            "session_json": session_json_str,
            "display_name": display_name,
            "country_code": country_code,
            "message": f"Successfully logged in{f' as {display_name}' if display_name else ''}. Session saved automatically.",
        }), flush=True)

    except Exception as exc:
        print(json.dumps({"type": "tidal_oauth_error", "message": f"OAuth login failed: {exc}"}), flush=True)


def _update_config_file(config_path: str | None, mutator) -> None:
    if not config_path or not os.path.exists(config_path):
        return
    with open(config_path, "r", encoding="utf-8") as f:
        current = json.load(f)
    mutator(current)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)


def _detect_windows_default_browser_family() -> str:
    if sys.platform != "win32":
        return "chrome"
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except Exception:
        return "chrome"

    lowered = str(prog_id or "").lower()
    if "mse" in lowered or "edge" in lowered:
        return "edge"
    if "brave" in lowered:
        return "brave"
    if "chrome" in lowered or "chromium" in lowered:
        return "chrome"
    return "chrome"


def _detect_macos_default_browser_family() -> str:
    """Read macOS default browser from LaunchServices plist."""
    try:
        import subprocess as _sp
        result = _sp.run(
            ["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure",
             "LSHandlers"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.lower()
        if "brave" in output:
            return "brave"
        if "microsoft.edge" in output or "msedge" in output:
            return "edge"
        if "google.chrome" in output or "googlechrome" in output:
            return "chrome"
    except Exception:
        pass
    return "chrome"


def _browser_candidate_specs() -> list[dict[str, str]]:
    platform = sys.platform  # "win32", "darwin", "linux"

    # ── Windows ───────────────────────────────────────────────────────────────
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", "")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        default_family = _detect_windows_default_browser_family()

        family_specs = {
            "chrome": {
                "label": "Chrome",
                "executables": [
                    os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
                ],
            },
            "edge": {
                "label": "Edge",
                "executables": [
                    os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
                ],
            },
            "brave": {
                "label": "Brave",
                "executables": [
                    os.path.join(program_files, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    os.path.join(program_files_x86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                ],
            },
        }

        ordered = [default_family] + [f for f in ("chrome", "edge", "brave") if f != default_family]
        specs: list[dict[str, str]] = []
        seen: set[str] = set()
        for family in ordered:
            spec = family_specs.get(family)
            if not spec:
                continue
            exe = next((p for p in spec["executables"] if p and os.path.exists(p)), "")
            key = exe or family
            if key in seen:
                continue
            seen.add(key)
            specs.append({"family": family, "label": spec["label"], "channel": "", "executable_path": exe})

        # Playwright-bundled Chromium as last resort (no executable_path needed)
        specs.append({"family": "chromium", "label": "Chromium", "channel": "", "executable_path": ""})
        return specs

    # ── macOS ─────────────────────────────────────────────────────────────────
    if platform == "darwin":
        home = os.path.expanduser("~")
        default_family = _detect_macos_default_browser_family()

        # Each entry: (family, label, list-of-candidate-paths)
        macos_browsers = [
            ("chrome", "Chrome", [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.join(home, "Applications", "Google Chrome.app", "Contents", "MacOS", "Google Chrome"),
            ]),
            ("brave", "Brave", [
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                os.path.join(home, "Applications", "Brave Browser.app", "Contents", "MacOS", "Brave Browser"),
            ]),
            ("edge", "Edge", [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                os.path.join(home, "Applications", "Microsoft Edge.app", "Contents", "MacOS", "Microsoft Edge"),
            ]),
            ("chromium", "Chromium", [
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                os.path.join(home, "Applications", "Chromium.app", "Contents", "MacOS", "Chromium"),
            ]),
        ]

        # Put default browser first
        ordered_families = [default_family] + [f for f, _, _ in macos_browsers if f != default_family]
        browser_map = {f: (lbl, paths) for f, lbl, paths in macos_browsers}

        specs = []
        seen = set()
        for family in ordered_families:
            if family not in browser_map:
                continue
            label, paths = browser_map[family]
            exe = next((p for p in paths if p and os.path.exists(p)), "")
            key = exe or family
            if key in seen:
                continue
            seen.add(key)
            specs.append({"family": family, "label": label, "channel": "", "executable_path": exe})

        # Playwright-bundled Chromium as last resort
        specs.append({"family": "chromium", "label": "Chromium (bundled)", "channel": "", "executable_path": ""})
        return specs

    # ── Linux ─────────────────────────────────────────────────────────────────
    # Use shutil.which() — browsers are on PATH on Linux.
    linux_browsers = [
        ("chrome",   "Chrome",   ["google-chrome", "google-chrome-stable", "google-chrome-beta"]),
        ("chromium", "Chromium", ["chromium-browser", "chromium"]),
        ("brave",    "Brave",    ["brave-browser", "brave"]),
        ("edge",     "Edge",     ["microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta"]),
    ]

    specs = []
    seen = set()
    for family, label, candidates in linux_browsers:
        exe = next((shutil.which(c) or "" for c in candidates if shutil.which(c)), "")
        if not exe:
            continue
        if exe in seen:
            continue
        seen.add(exe)
        specs.append({"family": family, "label": label, "channel": "", "executable_path": exe})

    # Playwright-bundled Chromium as last resort (always appended, even if no system browser found)
    specs.append({"family": "chromium", "label": "Chromium (bundled)", "channel": "", "executable_path": ""})
    return specs


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _get_playwright_chromium_exe() -> str:
    """
    Return the path to Playwright's downloaded Chromium executable.
    If it hasn't been downloaded yet, runs `playwright install chromium` first.
    Returns an empty string if Playwright is not installed at all.

    Playwright stores its browsers in a platform-specific cache:
      Windows : %LOCALAPPDATA%\\ms-playwright\\chromium-XXXX\\chrome-win64\\chrome.exe
      macOS   : ~/Library/Caches/ms-playwright/chromium-XXXX/chrome-mac/Chromium.app/...
      Linux   : ~/.cache/ms-playwright/chromium-XXXX/chrome-linux/chrome

    This function uses only stdlib (glob + os) — no playwright import needed.
    That keeps node.exe out of the PyInstaller bundle entirely.
    """
    import glob

    def _find_in_dir(base: str) -> str:
        """Glob for chromium-* subdirs and return the first valid executable."""
        if not base or not os.path.isdir(base):
            return ""
        if sys.platform == "win32":
            patterns = [
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"),
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-win", "chrome.exe"),
            ]
        elif sys.platform == "darwin":
            patterns = [
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-mac", "Chromium.app",
                             "Contents", "MacOS", "Chromium"),
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-mac-arm64", "Chromium.app",
                             "Contents", "MacOS", "Chromium"),
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-mac-x64", "Chromium.app",
                             "Contents", "MacOS", "Chromium"),
            ]
        else:
            patterns = [
                os.path.join(base, "ms-playwright", "chromium-*", "chrome-linux", "chrome"),
            ]
        for pattern in patterns:
            matches = sorted(glob.glob(pattern), reverse=True)  # newest version first
            for m in matches:
                if os.path.isfile(m) and os.access(m, os.X_OK if sys.platform != "win32" else os.F_OK):
                    return m
        return ""

    # Check platform-specific cache directories
    if sys.platform == "win32":
        search_dirs = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("USERPROFILE", ""),
        ]
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        search_dirs = [
            os.path.join(home, "Library", "Caches"),
            home,
        ]
    else:
        home = os.path.expanduser("~")
        search_dirs = [
            os.path.join(home, ".cache"),
            home,
        ]

    # Also check PLAYWRIGHT_BROWSERS_PATH env override
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env_path:
        search_dirs.insert(0, env_path)

    for base in search_dirs:
        exe = _find_in_dir(base)
        if exe:
            return exe

    # Not found — try to download it via `playwright install chromium`.
    # This only works when running from source (not a frozen PyInstaller binary),
    # because the frozen binary excludes the playwright package to save space.
    is_frozen = getattr(sys, "frozen", False)

    if is_frozen:
        # Inside the packaged app — can't run `playwright install` from the bundle.
        # Tell the user to install a Chromium-family browser instead.
        print(json.dumps({
            "type": "log",
            "level": "warning",
            "message": (
                "[Browser] No Chromium-family browser found. "
                "Please install Google Chrome, Chromium, or Brave to use the browser login feature. "
                "Download Chrome: https://www.google.com/chrome/"
            ),
        }), flush=True)
        return ""

    print(json.dumps({
        "type": "log",
        "level": "info",
        "message": "[Browser] Playwright Chromium not found — downloading now (one-time, ~150 MB)...",
    }), flush=True)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(json.dumps({
                "type": "log",
                "level": "warning",
                "message": f"[Browser] playwright install chromium failed: {result.stderr.strip()[:300]}",
            }), flush=True)
            return ""
    except Exception as e:
        print(json.dumps({
            "type": "log",
            "level": "warning",
            "message": f"[Browser] playwright install chromium error: {e}",
        }), flush=True)
        return ""

    # Search again after install
    for base in search_dirs:
        exe = _find_in_dir(base)
        if exe:
            print(json.dumps({
                "type": "log",
                "level": "info",
                "message": "[Browser] Playwright Chromium downloaded successfully.",
            }), flush=True)
            return exe

    return ""


def _launch_debug_browser_process(login_kind: str, start_url: str) -> dict[str, object]:
    """
    Launch a Chromium-family browser with remote debugging enabled so Playwright
    can connect to it via CDP.

    Priority:
      1. System browsers (Chrome, Brave, Edge, Chromium) — found via OS-specific
         paths on Windows/macOS or shutil.which() on Linux.
      2. Playwright's downloaded Chromium — auto-downloaded on first use if absent.

    Works on Windows, macOS, and Linux with or without a system browser installed.
    """
    last_error = None

    # ── 1. Try system browsers ────────────────────────────────────────────────
    for spec in _browser_candidate_specs():
        executable_path = str(spec.get("executable_path") or "").strip()
        if not executable_path or not os.path.exists(executable_path):
            continue
        port = _find_free_local_port()
        user_data_dir = tempfile.mkdtemp(prefix=f"antra_{spec['family']}_login_")
        cmd = [
            executable_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            start_url,
        ]
        try:
            proc = subprocess.Popen(cmd)
            return {
                "spec": spec,
                "proc": proc,
                "debug_port": port,
                "user_data_dir": user_data_dir,
                "playwright_managed": False,
            }
        except Exception as exc:
            last_error = exc
            shutil.rmtree(user_data_dir, ignore_errors=True)

    # ── 2. Playwright's Chromium (auto-downloaded if needed) ──────────────────
    pw_exe = _get_playwright_chromium_exe()
    if pw_exe:
        port = _find_free_local_port()
        user_data_dir = tempfile.mkdtemp(prefix="antra_pw_chromium_login_")
        cmd = [
            pw_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            start_url,
        ]
        try:
            proc = subprocess.Popen(cmd)
            return {
                "spec": {"family": "chromium", "label": "Chromium (Playwright)", "channel": ""},
                "proc": proc,
                "debug_port": port,
                "user_data_dir": user_data_dir,
                "playwright_managed": False,  # launched as subprocess, same cleanup path
            }
        except Exception as exc:
            last_error = exc
            shutil.rmtree(user_data_dir, ignore_errors=True)

    raise RuntimeError(
        f"Could not open any browser for {login_kind}. "
        "No Chromium-family browser was found. Install Google Chrome, Chromium, or Brave: https://www.google.com/chrome/ "
        "Please install Google Chrome, Chromium, or Brave. "
        f"Last error: {last_error}"
    )


def _launch_manual_browser_process(login_kind: str, start_url: str) -> dict[str, object]:
    """
    Launch a browser window for the user to log in manually (no CDP needed).
    Same priority order as _launch_debug_browser_process.
    Works on Windows, macOS, and Linux.
    """
    last_error = None

    # ── Try system browsers ───────────────────────────────────────────────────
    for spec in _browser_candidate_specs():
        executable_path = str(spec.get("executable_path") or "").strip()
        if not executable_path or not os.path.exists(executable_path):
            continue
        user_data_dir = tempfile.mkdtemp(prefix=f"antra_{spec['family']}_manual_")
        cmd = [
            executable_path,
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            start_url,
        ]
        try:
            proc = subprocess.Popen(cmd)
            return {
                "spec": spec,
                "proc": proc,
                "user_data_dir": user_data_dir,
                "playwright_managed": False,
            }
        except Exception as exc:
            last_error = exc
            shutil.rmtree(user_data_dir, ignore_errors=True)

    # ── 2. Playwright's Chromium (auto-downloaded if needed) ──────────────────
    pw_exe = _get_playwright_chromium_exe()
    if pw_exe:
        user_data_dir = tempfile.mkdtemp(prefix="antra_pw_chromium_manual_")
        cmd = [
            pw_exe,
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            start_url,
        ]
        try:
            proc = subprocess.Popen(cmd)
            return {
                "spec": {"family": "chromium", "label": "Chromium (Playwright)", "channel": ""},
                "proc": proc,
                "user_data_dir": user_data_dir,
                "playwright_managed": False,
            }
        except Exception as exc:
            last_error = exc
            shutil.rmtree(user_data_dir, ignore_errors=True)

    raise RuntimeError(
        f"Could not open any browser for {login_kind}. "
        "No Chromium-family browser was found. Install Google Chrome, Chromium, or Brave: https://www.google.com/chrome/ "
        "Please install Google Chrome, Chromium, or Brave. "
        f"Last error: {last_error}"
    )


def _cleanup_debug_browser(launch_state: dict[str, object]) -> None:
    proc = launch_state.get("proc")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    user_data_dir = str(launch_state.get("user_data_dir") or "").strip()
    if user_data_dir:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        shutil.rmtree(user_data_dir, ignore_errors=True)



# ── Lightweight CDP session (replaces playwright for browser login) ───────────
# Uses only `websockets` (pure Python, already in requirements) + stdlib.
# No Node.js, no playwright driver — saves ~86 MB from the final binary.
#
# Architecture:
#   CDPSession  — wraps a websockets connection to the browser-level WS endpoint.
#                 Multiplexes commands to any tab via sessionId.
#                 Subscribes to Target events to auto-attach new tabs.
#
# CDP commands used:
#   Target.setDiscoverTargets / Target.attachToTarget / Target.createTarget
#   Network.enable / Network.getCookies
#   Page.enable / Page.addScriptToEvaluateOnNewDocument / Page.navigate
#   Runtime.evaluate
#   Events: Network.requestWillBeSent, Target.attachedToTarget, Page.loadEventFired

import asyncio as _asyncio
import json as _json
import time as _time


class _CDPSession:
    """
    Single WebSocket connection to the browser debug endpoint.
    Multiplexes commands to multiple tabs via sessionId.
    """

    def __init__(self, ws):
        self._ws = ws
        self._msg_id = 0
        self._pending: dict[int, _asyncio.Future] = {}
        self._event_handlers: list = []   # [(method_pattern, callback)]
        self._session_handlers: dict[str, list] = {}  # sessionId → [(method, cb)]
        self._recv_task: _asyncio.Task | None = None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def start(self):
        loop = _asyncio.get_event_loop()
        self._recv_task = loop.create_task(self._recv_loop())

    async def stop(self):
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (Exception, _asyncio.CancelledError):
                pass
        try:
            await self._ws.close()
        except Exception:
            pass

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = _json.loads(raw)
                except Exception:
                    continue
                msg_id = msg.get("id")
                session_id = msg.get("sessionId", "")
                method = msg.get("method", "")

                # Response to a command
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                    continue

                # Event — dispatch to handlers
                for pattern, cb in list(self._event_handlers):
                    if pattern == "*" or method == pattern:
                        try:
                            if _asyncio.iscoroutinefunction(cb):
                                _asyncio.get_event_loop().create_task(cb(msg))
                            else:
                                cb(msg)
                        except Exception:
                            pass

                # Session-scoped event
                if session_id and session_id in self._session_handlers:
                    for pat, cb in list(self._session_handlers[session_id]):
                        if pat == "*" or method == pat:
                            try:
                                if _asyncio.iscoroutinefunction(cb):
                                    _asyncio.get_event_loop().create_task(cb(msg))
                                else:
                                    cb(msg)
                            except Exception:
                                pass
        except Exception:
            pass

    async def send(self, method: str, params: dict | None = None,
                   session_id: str = "", timeout: float = 10.0) -> dict:
        msg_id = self._next_id()
        payload: dict = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        loop = _asyncio.get_event_loop()
        fut: _asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        await self._ws.send(_json.dumps(payload))
        try:
            return await _asyncio.wait_for(fut, timeout=timeout)
        except _asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP command timed out: {method}")

    def on(self, method: str, callback):
        """Subscribe to a browser-level CDP event."""
        self._event_handlers.append((method, callback))

    def on_session(self, session_id: str, method: str, callback):
        """Subscribe to a session-scoped CDP event."""
        if session_id not in self._session_handlers:
            self._session_handlers[session_id] = []
        self._session_handlers[session_id].append((method, callback))

    def off_session(self, session_id: str):
        self._session_handlers.pop(session_id, None)


async def _cdp_connect(port: int, timeout: float = 30.0) -> "_CDPSession":
    """
    Wait for Chrome to expose its debug endpoint, then connect.
    Returns a CDPSession connected to the browser-level WebSocket.
    """
    import websockets
    import requests as _req

    endpoint = f"http://127.0.0.1:{port}"
    deadline = _time.monotonic() + timeout
    browser_ws_url = ""

    while _time.monotonic() < deadline:
        try:
            r = _req.get(f"{endpoint}/json/version", timeout=1)
            r.raise_for_status()
            data = r.json()
            browser_ws_url = data.get("webSocketDebuggerUrl", "")
            if browser_ws_url:
                break
        except Exception:
            pass
        await _asyncio.sleep(0.5)

    if not browser_ws_url:
        raise RuntimeError(f"Browser CDP endpoint never became ready on port {port}")

    ws = await websockets.connect(browser_ws_url, max_size=32 * 1024 * 1024)
    session = _CDPSession(ws)
    await session.start()
    return session


async def _cdp_get_page_tabs(port: int) -> list[dict]:
    """Return all page-type tabs from /json."""
    import requests as _req
    try:
        tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
        return [t for t in tabs if t.get("type") == "page"]
    except Exception:
        return []


async def _cdp_attach_tab(session: "_CDPSession", target_id: str) -> str:
    """Attach to a target and return its sessionId."""
    r = await session.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    sid = r.get("result", {}).get("sessionId", "")
    if not sid:
        raise RuntimeError(f"Could not attach to target {target_id}: {r.get('error')}")
    return sid


async def _cdp_setup_tab(session: "_CDPSession", sid: str, init_script: str = "") -> None:
    """Enable Network + Page domains and optionally inject an init script."""
    # maxPostDataSize=65536 ensures CDP includes POST body data in
    # Network.requestWillBeSent events — needed to capture customerId/deviceId
    # from Amazon's API request bodies.
    await session.send("Network.enable", {"maxPostDataSize": 65536}, session_id=sid)
    await session.send("Page.enable", {}, session_id=sid)
    if init_script:
        await session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": init_script},
            session_id=sid,
        )


async def _cdp_navigate(session: "_CDPSession", sid: str, url: str) -> None:
    await session.send("Page.navigate", {"url": url}, session_id=sid, timeout=30.0)


async def _cdp_evaluate(session: "_CDPSession", sid: str, expression: str) -> object:
    r = await session.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": False},
        session_id=sid,
        timeout=10.0,
    )
    return r.get("result", {}).get("result", {}).get("value")


async def _cdp_get_cookies(session: "_CDPSession", sid: str, urls: list[str]) -> list[dict]:
    r = await session.send("Network.getCookies", {"urls": urls}, session_id=sid)
    return r.get("result", {}).get("cookies", [])


# ── Amazon capture probe (same JS as before) ──────────────────────────────────

_AMAZON_PROBE_JS = r"""
(function() {
  const key = "__antraAmazonCapture";
  const store = window[key] = window[key] || {};
  const normalizeHeaders = (input) => {
    const out = {};
    if (!input) return out;
    try {
      if (input instanceof Headers) {
        input.forEach((value, name) => { out[String(name).toLowerCase()] = String(value); });
        return out;
      }
    } catch (_) {}
    try {
      if (Array.isArray(input)) {
        for (const pair of input) {
          if (Array.isArray(pair) && pair.length >= 2) out[String(pair[0]).toLowerCase()] = String(pair[1]);
        }
        return out;
      }
    } catch (_) {}
    try {
      for (const [name, value] of Object.entries(input)) out[String(name).toLowerCase()] = String(value);
    } catch (_) {}
    return out;
  };
  const record = (headers, body) => {
    const auth = headers["authorization"] || "";
    const csrf = headers["csrf-token"] || "";
    if (auth.startsWith("Bearer Atna|") && csrf) {
      store.authorization = auth;
      store.csrf_token = csrf;
      store.csrf_rnd = headers["csrf-rnd"] || store.csrf_rnd || "";
      store.csrf_ts = headers["csrf-ts"] || store.csrf_ts || "";
      store.device_id = headers["x-amzn-device-id"] || store.device_id || "";
      store.session_id = headers["x-amzn-session-id"] || store.session_id || "";
      try {
        const payload = typeof body === "string" ? JSON.parse(body) : body;
        if (payload && typeof payload === "object") {
          if (payload.customerId) store.customer_id = String(payload.customerId);
          const token = payload.deviceToken || {};
          if (token.deviceId) store.device_id = String(token.deviceId);
        }
      } catch (_) {}
    } else if (!store.session_id && headers["x-amzn-session-id"]) {
      store.session_id = headers["x-amzn-session-id"];
    }
  };
  if (!window.__antraAmazonProbeInstalled) {
    window.__antraAmazonProbeInstalled = true;
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      try {
        const init = args[1] || {};
        const headers = normalizeHeaders(init.headers);
        record(headers, init.body || "");
      } catch (_) {}
      return originalFetch(...args);
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(...args) {
      this.__antraHeaders = {};
      return originalOpen.apply(this, args);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
      try { this.__antraHeaders[String(name).toLowerCase()] = String(value); } catch (_) {}
      return originalSetRequestHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
      try { record(this.__antraHeaders || {}, body || ""); } catch (_) {}
      return originalSend.apply(this, arguments);
    };
  }
})();
"""

# ── Apple capture probe (same JS as before) ───────────────────────────────────

_APPLE_PROBE_JS = r"""
(function() {
  const key = "__antraAppleCapture";
  const store = window[key] = window[key] || {};
  const normalizeHeaders = (input) => {
    const out = {};
    if (!input) return out;
    try {
      if (input instanceof Headers) {
        input.forEach((value, name) => { out[String(name).toLowerCase()] = String(value); });
        return out;
      }
    } catch (_) {}
    try {
      if (Array.isArray(input)) {
        for (const pair of input) {
          if (Array.isArray(pair) && pair.length >= 2) out[String(pair[0]).toLowerCase()] = String(pair[1]);
        }
        return out;
      }
    } catch (_) {}
    try {
      for (const [name, value] of Object.entries(input)) out[String(name).toLowerCase()] = String(value);
    } catch (_) {}
    return out;
  };
  const record = (headers) => {
    const auth = headers["authorization"] || "";
    const mut = headers["music-user-token"] || headers["media-user-token"] || "";
    if (auth.startsWith("Bearer ") && mut) {
      store.authorization = auth;
      store.music_user_token = mut;
    }
  };
  if (!window.__antraAppleProbeInstalled) {
    window.__antraAppleProbeInstalled = true;
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      try {
        const init = args[1] || {};
        record(normalizeHeaders(init.headers));
      } catch (_) {}
      return originalFetch(...args);
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(...args) {
      this.__antraHeaders = {};
      return originalOpen.apply(this, args);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
      try { this.__antraHeaders[String(name).toLowerCase()] = String(value); } catch (_) {}
      return originalSetRequestHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
      try { record(this.__antraHeaders || {}); } catch (_) {}
      return originalSend.apply(this, arguments);
    };
  }
})();
"""


def _parse_request_headers_amazon(headers: dict) -> dict | None:
    """Extract Amazon auth fields from a request headers dict. Returns dict or None."""
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = h.get("authorization", "")
    csrf = h.get("csrf-token", "")
    if auth.startswith("Bearer Atna|") and csrf:
        return {
            "authorization": auth,
            "csrf_token": csrf,
            "csrf_rnd": h.get("csrf-rnd", ""),
            "csrf_ts": h.get("csrf-ts", ""),
            "device_id": h.get("x-amzn-device-id", ""),
            "session_id": h.get("x-amzn-session-id", ""),
        }
    return None


def _parse_request_headers_apple(headers: dict) -> dict | None:
    """Extract Apple Music auth fields from a request headers dict. Returns dict or None."""
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = h.get("authorization", "")
    mut = h.get("music-user-token", "") or h.get("media-user-token", "")
    if auth.startswith("Bearer ") and mut:
        return {"authorization": auth, "music_user_token": mut}
    return None


async def _cdp_read_probe(session: "_CDPSession", sid: str, store_key: str) -> dict:
    """Read the JS capture probe store from the page."""
    try:
        val = await _cdp_evaluate(
            session, sid,
            f"(function(){{ try {{ return JSON.stringify(window.{store_key} || {{}}); }} catch(e) {{ return '{{}}'; }} }})()"
        )
        parsed = _json.loads(val or "{}")
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v}
    except Exception:
        pass
    return {}


async def _cdp_find_music_tab(session: "_CDPSession", port: int,
                               host_fragment: str, timeout: float = 30.0) -> str:
    """
    Return the sessionId of the tab whose URL contains host_fragment.
    Polls /json (HTTP) since that's simpler than tracking Target events.
    """
    import requests as _req
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
            for tab in tabs:
                if tab.get("type") == "page" and host_fragment in (tab.get("url") or "").lower():
                    tid = tab["id"]
                    # Attach if not already attached
                    try:
                        sid = await _cdp_attach_tab(session, tid)
                        return sid
                    except Exception:
                        pass
        except Exception:
            pass
        await _asyncio.sleep(0.5)
    raise RuntimeError(f"Could not find a browser tab for {host_fragment} within {timeout}s")


async def _cdp_setup_all_tabs(session: "_CDPSession", port: int, probe_js: str,
                               on_request_headers) -> list[str]:
    """
    Attach to all existing page tabs, enable Network+Page, inject probe,
    and register a Network.requestWillBeSent handler on each.
    Returns list of sessionIds.
    """
    import requests as _req
    sids = []
    try:
        tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
        page_tabs = [t for t in tabs if t.get("type") == "page"]
    except Exception:
        page_tabs = []

    for tab in page_tabs:
        try:
            sid = await _cdp_attach_tab(session, tab["id"])
            await _cdp_setup_tab(session, sid, probe_js)
            # Register request event handler for this session
            def _make_handler(s):
                def _handler(msg):
                    if msg.get("method") == "Network.requestWillBeSent":
                        hdrs = msg["params"].get("request", {}).get("headers", {})
                        on_request_headers(hdrs, msg["params"])
                return _handler
            session.on_session(sid, "Network.requestWillBeSent", _make_handler(sid))
            sids.append(sid)
        except Exception:
            pass

    # Also subscribe to new tabs being created
    async def _on_attached(msg):
        if msg.get("method") != "Target.attachedToTarget":
            return
        info = msg.get("params", {}).get("targetInfo", {})
        if info.get("type") != "page":
            return
        new_sid = msg["params"].get("sessionId", "")
        if not new_sid or new_sid in sids:
            return
        try:
            await _cdp_setup_tab(session, new_sid, probe_js)
            def _make_handler2(s):
                def _handler(msg2):
                    if msg2.get("method") == "Network.requestWillBeSent":
                        hdrs = msg2["params"].get("request", {}).get("headers", {})
                        on_request_headers(hdrs, msg2["params"])
                return _handler
            session.on_session(new_sid, "Network.requestWillBeSent", _make_handler2(new_sid))
            sids.append(new_sid)
        except Exception:
            pass

    session.on("Target.attachedToTarget", _on_attached)
    # Enable auto-attach for new tabs
    await session.send("Target.setAutoAttach",
                       {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
    return sids


async def _capture_amazon_session_headless_async(playwright_cookies: list[dict]) -> dict:
    """
    Launch headless Chromium with pre-loaded Amazon cookies and capture auth headers.
    Uses CDP directly — no Playwright/Node.js required.
    """
    import requests as _req

    pw_exe = _get_playwright_chromium_exe()
    if not pw_exe:
        raise RuntimeError(
            "No Chromium executable found for headless capture. "
            "Run: playwright install chromium"
        )

    cookie_header = _build_amazon_cookie_header(playwright_cookies)
    if not cookie_header:
        raise RuntimeError("Could not build Amazon cookie header from browser session.")

    port = _find_free_local_port()
    udd = tempfile.mkdtemp(prefix="antra_amz_headless_")
    proc = subprocess.Popen(
        [pw_exe,
         f"--remote-debugging-port={port}",
         f"--user-data-dir={udd}",
         "--headless=new",
         "--no-first-run", "--no-default-browser-check",
         "--no-sandbox", "--disable-dev-shm-usage",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    captured: dict = {}
    try:
        session = await _cdp_connect(port, timeout=20.0)
        try:
            # Get the initial tab
            tabs = []
            for _ in range(20):
                try:
                    tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=1).json()
                    page_tabs = [t for t in tabs if t.get("type") == "page"]
                    if page_tabs:
                        break
                except Exception:
                    pass
                await _asyncio.sleep(0.3)

            if not page_tabs:
                raise RuntimeError("Headless browser has no page tabs")

            sid = await _cdp_attach_tab(session, page_tabs[0]["id"])
            await _cdp_setup_tab(session, sid)

            def _on_req(hdrs, params):
                if captured.get("authorization"):
                    return
                result = _parse_request_headers_amazon(hdrs)
                if result:
                    captured.update(result)
                    try:
                        body = _json.loads(params.get("request", {}).get("postData") or "{}")
                        if isinstance(body, dict):
                            if body.get("customerId"):
                                captured["customer_id"] = str(body["customerId"])
                            dt = body.get("deviceToken") or {}
                            if isinstance(dt, dict) and dt.get("deviceId"):
                                captured["device_id"] = str(dt["deviceId"])
                    except Exception:
                        pass
                elif not captured.get("session_id"):
                    h = {str(k).lower(): str(v) for k, v in hdrs.items()}
                    if h.get("x-amzn-session-id"):
                        captured["session_id"] = h["x-amzn-session-id"]

            session.on_session(sid, "Network.requestWillBeSent", lambda msg: _on_req(
                msg["params"].get("request", {}).get("headers", {}), msg["params"]
            ))

            # Set cookies via extra headers (simpler than CDP Network.setCookies for headless)
            await session.send("Network.setExtraHTTPHeaders",
                               {"headers": {"Cookie": cookie_header}}, session_id=sid)

            await _cdp_navigate(session, sid, "https://music.amazon.com/home")

            for _ in range(30):
                if captured.get("authorization"):
                    break
                await _asyncio.sleep(0.5)

            if not captured.get("authorization"):
                await _cdp_navigate(session, sid, "https://music.amazon.com/")
                for _ in range(20):
                    if captured.get("authorization"):
                        break
                    await _asyncio.sleep(0.5)

            # JS fallback for customer_id
            if not captured.get("customer_id"):
                try:
                    cid = await _cdp_evaluate(session, sid,
                        "(function(){ try { return (window.amznMusic && window.amznMusic.appConfig && window.amznMusic.appConfig.customerId) || (window.__PRELOADED_STATE__ && window.__PRELOADED_STATE__.Authentication && window.__PRELOADED_STATE__.Authentication.customerId) || ''; } catch(e) { return ''; } })()")
                    if cid:
                        captured["customer_id"] = str(cid)
                except Exception:
                    pass

            # Get updated cookies
            updated = await _cdp_get_cookies(session, sid,
                ["https://music.amazon.com", "https://www.amazon.com", "https://amazon.com"])
            captured["cookie"] = "; ".join(
                f"{c['name']}={c['value']}" for c in updated if c.get("name") and c.get("value")
            )
        finally:
            await session.stop()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(udd, ignore_errors=True)

    if not captured.get("authorization"):
        raise RuntimeError(
            "No Amazon Music auth headers captured in headless session. "
            "Make sure you are fully signed in to Amazon Music."
        )
    return captured

def _run_apple_browser_login(cfg, config_path: str | None) -> None:
    async def _runner():
        print(json.dumps({
            "type": "apple_login_status",
            "message": "Opening your browser for Apple Music login...",
        }), flush=True)

        launch_state = _launch_debug_browser_process("Apple Music", "https://music.apple.com/us/browse")
        port = int(launch_state["debug_port"])
        browser_spec = launch_state["spec"]
        captured: dict[str, str] = {}

        try:
            session = await _cdp_connect(port, timeout=30.0)
            try:
                # Attach to all existing tabs + auto-attach new ones
                def _on_req_hdrs(hdrs, params):
                    result = _parse_request_headers_apple(hdrs)
                    if result:
                        captured.update(result)

                await _cdp_setup_all_tabs(session, port, _APPLE_PROBE_JS, _on_req_hdrs)

                print(json.dumps({
                    "type": "apple_login_status",
                    "message": (
                        f"Sign in to Apple Music in {browser_spec['label']}. "
                        "Antra will capture your session automatically and return here."
                    ),
                }), flush=True)

                # Poll for up to 3 minutes
                for tick in range(180):
                    if captured.get("authorization") and captured.get("music_user_token"):
                        break
                    # Every 30s reload the music tab to trigger fresh API calls
                    if tick in (30, 60, 90):
                        try:
                            sid = await _cdp_find_music_tab(session, port, "music.apple.com", timeout=3.0)
                            await _cdp_navigate(session, sid, "https://music.apple.com/us/browse")
                        except Exception:
                            pass
                    # Also try reading the JS probe
                    if tick % 5 == 0:
                        try:
                            sid = await _cdp_find_music_tab(session, port, "music.apple.com", timeout=2.0)
                            probe = await _cdp_read_probe(session, sid, "__antraAppleCapture")
                            for k, v in probe.items():
                                if v:
                                    captured[k] = v
                        except Exception:
                            pass
                    await _asyncio.sleep(1)

                # Extract storefront from tab URL
                storefront = "us"
                try:
                    import requests as _req
                    tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
                    for tab in tabs:
                        url = tab.get("url") or ""
                        m = re.search(r"music\.apple\.com/([a-z]{2})/", url)
                        if m:
                            storefront = m.group(1).lower()
                            break
                except Exception:
                    pass

                if not captured.get("authorization") or not captured.get("music_user_token"):
                    raise RuntimeError(
                        "No Apple Music session headers were captured. "
                        "Make sure you completed sign-in and the main Apple Music page loaded."
                    )

                _update_config_file(config_path, lambda current: current.update({
                    "apple_enabled": True,
                    "apple_authorization_token": captured["authorization"],
                    "apple_music_user_token": captured["music_user_token"],
                    "apple_storefront": storefront,
                }))

                # Push tokens to VPS mirror if configured
                vps_message = "Apple Music session captured and saved."
                vps_ok = False
                try:
                    from antra.core.endpoint_manifest import load_endpoint_manifest
                    manifest = load_endpoint_manifest()
                    mirror_apple = manifest.mirror_apple
                    api_key = cfg.antra_api_key or manifest.api_key

                    if mirror_apple and api_key:
                        import requests as _req
                        resp = _req.post(
                            f"{mirror_apple}/api/refresh-token",
                            json={
                                "authorization": captured["authorization"],
                                "media_user_token": captured["music_user_token"],
                                "storefront": storefront,
                                "account": 1,
                            },
                            headers={"X-Api-Key": api_key},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            vps_ok = True
                            vps_message = "Token captured and pushed to VPS server."
                        else:
                            vps_message = f"Token captured locally. VPS push failed ({resp.status_code}) — update manually."
                    elif mirror_apple:
                        vps_message = "Token captured locally. No API key set — VPS push skipped."
                    else:
                        vps_message = "Token captured and saved locally."
                except Exception as _vps_err:
                    vps_message = f"Token captured locally. VPS push error: {_vps_err}"

                print(json.dumps({
                    "type": "apple_login_success",
                    "authorization_token": captured["authorization"],
                    "music_user_token": captured["music_user_token"],
                    "storefront": storefront,
                    "vps_ok": vps_ok,
                    "message": vps_message,
                }), flush=True)
            finally:
                await session.stop()
        finally:
            _cleanup_debug_browser(launch_state)

    try:
        import asyncio
        asyncio.run(_runner())
    except Exception as exc:
        print(json.dumps({
            "type": "apple_login_error",
            "message": f"Apple Music login failed: {exc}",
        }), flush=True)


# ── Amazon Music login helpers ────────────────────────────────────────────────

def _amazon_sentinel_path() -> str:
    """Sentinel file that Go writes when the user clicks 'I'm Signed In'."""
    return os.path.join(tempfile.gettempdir(), "antra_amazon_login_confirm.tmp")


def _build_amazon_cookie_header(cookies: list[dict]) -> str:
    def _clean_token(text: str) -> str:
        return "".join(ch for ch in text if 0x20 < ord(ch) < 0x7F and ch not in {";", ",", " "})

    def _clean_value(text: str) -> str:
        return "".join(ch for ch in text if ch not in {"\r", "\n", "\x00"})

    deduped: dict[str, str] = {}
    for cookie in cookies:
        name = _clean_token(str(cookie.get("name") or "").strip())
        value = _clean_value(str(cookie.get("value") or ""))
        if not name or not value:
            continue
        deduped[name] = value

    return "; ".join(f"{name}={value}" for name, value in deduped.items())


def _build_amazon_cookie_string_from_context(cookies: list[dict]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or not value or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _extract_cookie_value(cookie_string: str, name: str) -> str:
    target = (name or "").strip()
    if not target:
        return ""
    for part in (cookie_string or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip() == target:
            return value.strip()
    return ""


def _run_amazon_browser_login(cfg, config_path):
    import asyncio

    _sentinel = _amazon_sentinel_path()
    try:
        os.unlink(_sentinel)
    except Exception:
        pass

    async def _runner():
        print(json.dumps({
            "type": "amazon_login_status",
            "phase": "starting",
            "message": "Opening your browser for Amazon Music login...",
        }), flush=True)

        # Use the configured regional storefront URL
        from antra.sources.amazon import _DirectAmazonClient
        region = (getattr(cfg, "amazon_region", "us") or "us").strip().lower()
        start_url = _DirectAmazonClient._STOREFRONT_URLS.get(region, "https://music.amazon.com/")

        launch_state = _launch_debug_browser_process("Amazon Music", start_url)
        port = int(launch_state["debug_port"])
        browser_spec = launch_state["spec"]
        captured = {}

        try:
            session = await _cdp_connect(port, timeout=30.0)
            try:
                def _on_req_hdrs(hdrs, params):
                    result = _parse_request_headers_amazon(hdrs)
                    if result:
                        for k, v in result.items():
                            if v and not captured.get(k):
                                captured[k] = v
                        # postData is included when Network.enable maxPostDataSize is set
                        post_data = params.get("request", {}).get("postData") or ""
                        if post_data:
                            try:
                                body = json.loads(post_data)
                                if isinstance(body, dict):
                                    if body.get("customerId") and not captured.get("customer_id"):
                                        captured["customer_id"] = str(body["customerId"])
                                    dt = body.get("deviceToken") or {}
                                    if isinstance(dt, dict) and dt.get("deviceId") and not captured.get("device_id"):
                                        captured["device_id"] = str(dt["deviceId"])
                            except Exception:
                                pass
                    else:
                        h = {str(k).lower(): str(v) for k, v in hdrs.items()}
                        if h.get("x-amzn-session-id") and not captured.get("session_id"):
                            captured["session_id"] = h["x-amzn-session-id"]

                await _cdp_setup_all_tabs(session, port, _AMAZON_PROBE_JS, _on_req_hdrs)

                print(json.dumps({
                    "type": "amazon_login_status",
                    "phase": "waiting_for_user",
                    "message": (
                        "Sign in to Amazon Music in " + browser_spec["label"] + ". "
                        "Play a song if needed, then click 'I'm Signed In' in Antra."
                    ),
                }), flush=True)

                deadline = time.time() + 600
                confirmed = False
                while time.time() < deadline:
                    if os.path.exists(_sentinel):
                        try:
                            os.unlink(_sentinel)
                        except Exception:
                            pass
                        confirmed = True
                        break
                    await _asyncio.sleep(1)

                if not confirmed:
                    raise RuntimeError("Amazon Music login timed out (10 minutes). Please try again.")

                print(json.dumps({
                    "type": "amazon_login_status",
                    "phase": "capturing",
                    "message": "Reading your browser session — please wait...",
                }), flush=True)

                for attempt in range(90):
                    if captured.get("authorization") and captured.get("csrf_token"):
                        break
                    if attempt % 5 == 0:
                        try:
                            sid = await _cdp_find_music_tab(session, port, "music.amazon.com", timeout=2.0)
                            probe = await _cdp_read_probe(session, sid, "__antraAmazonCapture")
                            for k, v in probe.items():
                                if v and not captured.get(k):
                                    captured[k] = v
                        except Exception:
                            pass
                    if attempt in (0, 2, 5, 10, 20, 35):
                        try:
                            sid = await _cdp_find_music_tab(session, port, "music.amazon.com", timeout=2.0)
                            await _cdp_navigate(session, sid, start_url + "home")
                        except Exception:
                            pass
                    await _asyncio.sleep(1)

                if not captured.get("authorization") or not captured.get("csrf_token"):
                    raise RuntimeError(
                        "No Amazon Music auth headers were captured. "
                        "Make sure you are fully signed in, play any track, then click 'I'm Signed In'."
                    )

                # JS fallbacks for customer_id and device_id if not captured from request bodies
                try:
                    sid = await _cdp_find_music_tab(session, port, "music.amazon.com", timeout=5.0)
                    if not captured.get("customer_id"):
                        cid = await _cdp_evaluate(session, sid,
                            "(function(){ try {"
                            " return (window.amznMusic && window.amznMusic.appConfig && window.amznMusic.appConfig.customerId)"
                            " || (window.__PRELOADED_STATE__ && window.__PRELOADED_STATE__.Authentication && window.__PRELOADED_STATE__.Authentication.customerId)"
                            " || (window.ue_mid) || '';"
                            " } catch(e) { return ''; } })()")
                        if cid:
                            captured["customer_id"] = str(cid)
                    if not captured.get("device_id"):
                        did = await _cdp_evaluate(session, sid,
                            "(function(){ try {"
                            " return (window.amznMusic && window.amznMusic.appConfig && window.amznMusic.appConfig.deviceId)"
                            " || (window.__PRELOADED_STATE__ && window.__PRELOADED_STATE__.Authentication && window.__PRELOADED_STATE__.Authentication.deviceId)"
                            " || '';"
                            " } catch(e) { return ''; } })()")
                        if did:
                            captured["device_id"] = str(did)
                except Exception:
                    pass

                try:
                    sid = await _cdp_find_music_tab(session, port, "music.amazon.com", timeout=5.0)
                    # Collect cookies from both the regional domain and the base amazon.com
                    cookie_urls = [start_url.rstrip("/"), "https://www.amazon.com", "https://amazon.com"]
                    cookies = await _cdp_get_cookies(session, sid, cookie_urls)
                    cookie_string = _build_amazon_cookie_string_from_context(cookies)
                except Exception:
                    cookie_string = ""

                if not cookie_string:
                    raise RuntimeError("Amazon Music login succeeded, but no browser cookies were available to save.")

                captured["cookie"] = cookie_string
                return captured
            finally:
                await session.stop()
        finally:
            _cleanup_debug_browser(launch_state)

    try:
        _captured = asyncio.run(_runner())
    except Exception as _exc:
        print(json.dumps({
            "type": "amazon_login_error",
            "message": "Amazon Music login failed: " + str(_exc),
        }), flush=True)
        return

    _existing_wvd = ""
    _existing_json = ""
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                _current_cfg = json.load(f)
            if isinstance(_current_cfg, dict):
                _existing_wvd = str(_current_cfg.get("amazon_wvd_path") or "").strip()
                _existing_json = str(_current_cfg.get("amazon_direct_creds_json") or "").strip()
        except Exception:
            pass
    if not _existing_wvd:
        _existing_wvd = (getattr(cfg, "amazon_wvd_path", "") or "").strip()
    if not _existing_wvd and _existing_json:
        try:
            _parsed = json.loads(_existing_json)
            if isinstance(_parsed, dict):
                _existing_wvd = str(_parsed.get("wvd_path") or "").strip()
        except Exception:
            pass

    if not _captured.get("session_id"):
        _captured["session_id"] = _extract_cookie_value(_captured.get("cookie", ""), "session-id")

    _creds_payload = {
        "cookie":        _captured.get("cookie", ""),
        "authorization": _captured.get("authorization", ""),
        "csrf_token":    _captured.get("csrf_token", ""),
        "csrf_rnd":      _captured.get("csrf_rnd", ""),
        "csrf_ts":       _captured.get("csrf_ts", ""),
        "customer_id":   _captured.get("customer_id", ""),
        "device_id":     _captured.get("device_id", ""),
        "session_id":    _captured.get("session_id", ""),
        "wvd_path":      _existing_wvd,
    }

    _update_config_file(config_path, lambda current: current.update({
        "amazon_enabled": True,
        "amazon_wvd_path": _existing_wvd,
        "amazon_direct_creds_json": json.dumps(_creds_payload),
    }))

    _message = "Amazon Music session captured and saved automatically."
    if not _existing_wvd:
        _message += " Add your Widevine device path in Settings to enable downloads."

    print(json.dumps({
        "type": "amazon_login_success",
        "direct_creds_json": json.dumps(_creds_payload),
        "has_wvd_path": bool(_existing_wvd),
        "message": _message,
    }), flush=True)

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
    parser.add_argument("--retry-track-json", metavar="TRACK_JSON",
                        help="Retry a single previously failed track from serialized track metadata")
    parser.add_argument("--discovery-json", action="store_true",
                        help="Fetch discovery data (Top Albums/Playlists) and exit")
    parser.add_argument("--discovery-region", default="us",
                        help="Storefront region for discovery data")
    parser.add_argument("--discovery-genre-id", default="",
                        help="Genre ID for discovery data")
    parser.add_argument("--discovery-genre-name", default="",
                        help="Genre name for discovery data")
    parser.add_argument("--discovery-genres-only", action="store_true",
                        help="Only fetch list of genres and exit")
    parser.add_argument("--tidal-validate", action="store_true",
                        help="Validate configured TIDAL premium session data and exit")
    parser.add_argument("--tidal-oauth-login", action="store_true",
                        help="Start TIDAL OAuth device-code login flow and emit events, then exit")
    parser.add_argument("--apple-browser-login", action="store_true",
                        help="Open a browser and capture the Apple Music session automatically")
    parser.add_argument("--amazon-browser-login", action="store_true",
                        help="Open a browser and capture the Amazon Music session automatically")
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
                from antra.core.endpoint_manifest import load_endpoint_manifest
                from antra.core.amazon_music_fetcher import AmazonMusicFetcher
                cfg = load_config()
                manifest = load_endpoint_manifest()
                fetcher = AmazonMusicFetcher(mirrors=cfg.amazon_mirrors or manifest.amazon)
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

    if args.discovery_json:
        try:
            from antra.core.discovery import AppleDiscovery
            d = AppleDiscovery()
            data = d.get_discovery_data(
                storefront=args.discovery_region,
                genre_id=args.discovery_genre_id if args.discovery_genre_id else None,
                genre_name=args.discovery_genre_name if args.discovery_genre_name else None
            )
            print(json.dumps({"type": "discovery", "data": data}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.discovery_genres_only:
        try:
            from antra.core.discovery import AppleDiscovery
            d = AppleDiscovery()
            genres = d.get_genres(storefront=args.discovery_region)
            print(json.dumps({"type": "discovery_genres", "data": genres}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.probe:
        from antra.utils.runtime import get_ffprobe_exe, get_clean_subprocess_env
        import subprocess
        ffprobe = get_ffprobe_exe()
        probe_ok = False
        if ffprobe:
            try:
                r = subprocess.run(
                    [ffprobe, "-v", "quiet", "-print_format", "json",
                     "-show_format", "-show_streams", "-select_streams", "a:0", args.probe],
                    capture_output=True, timeout=30,
                    env=get_clean_subprocess_env(),
                )
                if r.returncode == 0 and r.stdout.strip():
                    print(r.stdout.decode("utf-8", errors="replace"), flush=True)
                    probe_ok = True
            except Exception:
                pass
        if not probe_ok:
            # ffprobe unavailable or returned an error — fall back to mutagen
            result = _probe_via_mutagen(args.probe)
            print(json.dumps(result), flush=True)
        sys.exit(0)

    if args.spectrogram:
        from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env
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
                env=get_clean_subprocess_env(),
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
            if settings.get("spotify_sp_dc"):
                os.environ["SPOTIFY_SP_DC"] = settings["spotify_sp_dc"]
            if "soulseek_enabled" in settings:
                # Also treat sources_enabled: ['soulseek'] as requesting Soulseek even if the
                # soulseek_enabled flag is false — this happens when the user unchecks the HiFi
                # source group without explicitly toggling the Soulseek switch (the Soulseek
                # checkbox appears checked because sourcesAll was true, but soulseek_enabled was
                # never flipped, leaving it at its false default).
                sources_has_soulseek = "soulseek" in (settings.get("sources_enabled") or [])
                soulseek_on = bool(settings["soulseek_enabled"]) or sources_has_soulseek
                os.environ["SLSKD_AUTO_BOOTSTRAP"] = "true" if soulseek_on else "false"
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

            if "qobuz_enabled" in settings:
                os.environ["QOBUZ_ENABLED"] = "true" if settings["qobuz_enabled"] else "false"
            if settings.get("qobuz_email") is not None:
                os.environ["QOBUZ_EMAIL"] = str(settings.get("qobuz_email") or "")
            if settings.get("qobuz_password") is not None:
                os.environ["QOBUZ_PASSWORD"] = str(settings.get("qobuz_password") or "")
            if settings.get("qobuz_app_id") is not None:
                os.environ["QOBUZ_APP_ID"] = str(settings.get("qobuz_app_id") or "285473059")
            if settings.get("qobuz_app_secret") is not None:
                os.environ["QOBUZ_APP_SECRET"] = str(settings.get("qobuz_app_secret") or "")
            if settings.get("qobuz_user_auth_token") is not None:
                os.environ["QOBUZ_USER_AUTH_TOKEN"] = str(settings.get("qobuz_user_auth_token") or "")

            if settings.get("deezer_arl_token") is not None:
                os.environ["DEEZER_ARL_TOKEN"] = str(settings.get("deezer_arl_token") or "")
            if settings.get("deezer_bf_secret") is not None:
                os.environ["DEEZER_BF_SECRET"] = str(settings.get("deezer_bf_secret") or "g4el58wc0zvf9na1")

            if "tidal_enabled" in settings:
                os.environ["TIDAL_ENABLED"] = "true" if settings["tidal_enabled"] else "false"
            if settings.get("tidal_auth_mode") is not None:
                os.environ["TIDAL_AUTH_MODE"] = str(settings.get("tidal_auth_mode") or "session_json")
            if settings.get("tidal_session_json") is not None:
                os.environ["TIDAL_SESSION_JSON"] = str(settings.get("tidal_session_json") or "")
            if settings.get("tidal_access_token") is not None:
                os.environ["TIDAL_ACCESS_TOKEN"] = str(settings.get("tidal_access_token") or "")
            if settings.get("tidal_refresh_token") is not None:
                os.environ["TIDAL_REFRESH_TOKEN"] = str(settings.get("tidal_refresh_token") or "")
            if settings.get("tidal_session_id") is not None:
                os.environ["TIDAL_SESSION_ID"] = str(settings.get("tidal_session_id") or "")
            if settings.get("tidal_token_type") is not None:
                os.environ["TIDAL_TOKEN_TYPE"] = str(settings.get("tidal_token_type") or "Bearer")
            if settings.get("tidal_country_code") is not None:
                os.environ["TIDAL_COUNTRY_CODE"] = str(settings.get("tidal_country_code") or "")

            if "apple_enabled" in settings:
                os.environ["APPLE_ENABLED"] = "true" if settings["apple_enabled"] else "false"
            if settings.get("apple_authorization_token") is not None:
                os.environ["APPLE_AUTHORIZATION_TOKEN"] = str(settings.get("apple_authorization_token") or "")
            if settings.get("apple_music_user_token") is not None:
                os.environ["APPLE_MUSIC_USER_TOKEN"] = str(settings.get("apple_music_user_token") or "")
            if settings.get("apple_storefront") is not None:
                os.environ["APPLE_STOREFRONT"] = str(settings.get("apple_storefront") or "us")
            if settings.get("apple_wvd_path") is not None:
                os.environ["APPLE_WVD_PATH"] = str(settings.get("apple_wvd_path") or "")

            if "amazon_enabled" in settings:
                os.environ["AMAZON_ENABLED"] = "true" if settings["amazon_enabled"] else "false"
            if settings.get("amazon_direct_creds_json") is not None:
                os.environ["AMAZON_DIRECT_CREDS_JSON"] = str(settings.get("amazon_direct_creds_json") or "")
            if settings.get("amazon_wvd_path") is not None:
                os.environ["AMAZON_WVD_PATH"] = str(settings.get("amazon_wvd_path") or "")

            if "output_format" in settings and settings["output_format"]:
                os.environ["OUTPUT_FORMAT"] = settings["output_format"]
            if "max_retries" in settings:
                os.environ["MAX_RETRIES"] = str(settings["max_retries"])

            if "soulseek_seed_after_download" in settings:
                os.environ["SOULSEEK_SEED_AFTER_DOWNLOAD"] = "true" if settings["soulseek_seed_after_download"] else "false"

            # sources_enabled: ["hifi", "soulseek"] controls which adapter groups are active.
            # Empty or absent = all enabled (auto).
            sources_enabled = settings.get("sources_enabled") or []
            if sources_enabled:
                os.environ["SOURCES_ENABLED"] = ",".join(sources_enabled)
            else:
                os.environ["SOURCES_ENABLED"] = ""

            os.environ["LIBRARY_MODE"] = settings.get("library_mode") or "smart_dedup"

            # Always set these env vars explicitly (even to their default values) so
            # load_dotenv's override=True (which runs at import time) cannot leave a
            # stale value from a .env file if the config key is absent.
            os.environ["FOLDER_STRUCTURE"] = settings.get("folder_structure") or "standard"
            os.environ["ALBUM_FOLDER_STRUCTURE"] = settings.get("album_folder_structure") or settings.get("folder_structure") or "standard"
            os.environ["PLAYLIST_FOLDER_STRUCTURE"] = settings.get("playlist_folder_structure") or settings.get("folder_structure") or "standard"
            os.environ["SINGLE_TRACK_STRUCTURE"] = settings.get("single_track_structure") or "album_numbered"
            os.environ["FILENAME_FORMAT"] = settings.get("filename_format") or "default"

            if "prefer_explicit" in settings:
                os.environ["PREFER_EXPLICIT"] = "true" if settings["prefer_explicit"] else "false"

            # API key for self-hosted mirror servers
            if settings.get("antra_api_key") is not None:
                os.environ["ANTRA_API_KEY"] = str(settings.get("antra_api_key") or "")


        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": f"Failed to load config: {e}"}))

    # Source preference is now driven by sources_enabled; keep auto as the resolver default.
    os.environ["SOURCE_PREFERENCE"] = "auto"

    from antra.core.config import load_config
    from antra.utils.organizer import LibraryOrganizer
    cfg = load_config()

    if args.tidal_validate:
        print(json.dumps(_validate_tidal_auth(cfg)), flush=True)
        sys.exit(0)

    if args.tidal_oauth_login:
        _run_tidal_oauth_login(cfg, args.config)
        sys.exit(0)

    if args.apple_browser_login:
        _run_apple_browser_login(cfg, args.config)
        sys.exit(0)

    if args.amazon_browser_login:
        _run_amazon_browser_login(cfg, args.config)
        sys.exit(0)

    print(json.dumps({"type": "log", "level": "info", "message": f"[Config] Filename format: {cfg.filename_format} | Album layout: {getattr(cfg, 'album_folder_structure', cfg.folder_structure)} | Playlist layout: {getattr(cfg, 'playlist_folder_structure', cfg.folder_structure)} | Single layout: {getattr(cfg, 'single_track_structure', 'album_numbered')} | Output format: {cfg.output_format}"}))

    service = AntraService(cfg)
    options = RuntimeOptions(
        output_dir=cfg.output_dir,
        source_preference="auto",
        output_format=cfg.output_format
    )

    if args.retry_track_json:
        try:
            organizer = LibraryOrganizer(
                cfg.output_dir,
                full_albums=getattr(cfg, "library_mode", "smart_dedup") == "full_albums",
                folder_structure=getattr(cfg, "folder_structure", "standard"),
                album_folder_structure=getattr(cfg, "album_folder_structure", getattr(cfg, "folder_structure", "standard")),
                playlist_folder_structure=getattr(cfg, "playlist_folder_structure", getattr(cfg, "folder_structure", "standard")),
                single_track_structure=getattr(cfg, "single_track_structure", "album_numbered"),
                filename_format=getattr(cfg, "filename_format", "default"),
            )
        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": f"Cannot access output directory: {e}"}))
            print(json.dumps({"type": "done"}))
            return

        try:
            track = _track_from_payload(json.loads(args.retry_track_json))
            print(json.dumps({
                "type": "log",
                "level": "info",
                "message": f"Retrying failed track: {track.artist_string} - {track.title}",
            }), flush=True)
            service.download_tracks(
                [track],
                options=options,
                event_callback=emit_event,
                organizer=organizer,
            )
        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": str(e)}), flush=True)

        print(json.dumps({"type": "done"}), flush=True)
        return

    if not args.playlists:
        print(json.dumps({"type": "log", "level": "error", "message": "No playlists provided"}))
        return

    playlists_to_run = args.playlists

    # Build the organizer once — it scans the entire library on init, which can
    # be very slow on a NAS. Reusing one instance across all URLs in this batch
    # means the scan happens exactly once per run, not once per URL.
    try:
        organizer = LibraryOrganizer(
            cfg.output_dir,
            full_albums=getattr(cfg, "library_mode", "smart_dedup") == "full_albums",
            folder_structure=getattr(cfg, "folder_structure", "standard"),
            album_folder_structure=getattr(cfg, "album_folder_structure", getattr(cfg, "folder_structure", "standard")),
            playlist_folder_structure=getattr(cfg, "playlist_folder_structure", getattr(cfg, "folder_structure", "standard")),
            single_track_structure=getattr(cfg, "single_track_structure", "album_numbered"),
            filename_format=getattr(cfg, "filename_format", "default"),
        )
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error", "message": f"Cannot access output directory: {e}"}))
        print(json.dumps({"type": "done"}))
        return

    print(json.dumps({"type": "log", "level": "info", "message": f"Preparing library update for {len(playlists_to_run)} source(s)"}))

    import os as _os
    from datetime import datetime

    for url in playlists_to_run:
        _url_start = time.time()

        # ── Spotify podcast episode / show ────────────────────────────────────
        from antra.core.podcast import is_podcast_url
        if is_podcast_url(url):
            try:
                print(json.dumps({"type": "log", "level": "info",
                                  "message": f"Podcast URL detected — fetching episodes: {url}"}), flush=True)
                summary = _download_podcast_url(url, cfg, cfg.output_dir)
                print(json.dumps({"type": "library_update",
                                  "tracks_added": summary["downloaded"], "url": url}), flush=True)
                print(json.dumps(summary), flush=True)
            except Exception as e:
                error_msg = str(e)
                print(json.dumps({"type": "log", "level": "error", "message": error_msg}), flush=True)
                from datetime import datetime
                print(json.dumps({
                    "type": "playlist_summary", "url": url, "title": "", "artwork_url": "",
                    "total": 0, "downloaded": 0, "failed": 0, "skipped": 0,
                    "error": error_msg, "sources": {},
                    "date": datetime.now().isoformat(), "total_mb": 0,
                    "elapsed_seconds": round(time.time() - _url_start),
                }), flush=True)
            continue
        # ─────────────────────────────────────────────────────────────────────

        try:
            print(json.dumps({"type": "log", "level": "info", "message": f"Syncing playlist to library: {url}"}))
            tracks = service.fetch_playlist_tracks(url, options=options, enrich_override=False)

            # Emit the full tracklist immediately after metadata is fetched,
            # before any individual downloads begin.
            if tracks:
                # Prefer playlist-level artwork (distinct cover) over track album art
                _artwork_early = (
                    getattr(tracks[0], "playlist_artwork_url", None)
                    or getattr(tracks[0], "artwork_url", None)
                    or ""
                )
                _title_early = (
                    getattr(tracks[0], "playlist_name", None)
                    or getattr(tracks[0], "album", None)
                    or ""
                )
                _quality_badge_map = {
                    'lossless': 'LOSSLESS', 'flac': 'LOSSLESS',
                    'alac': 'ALAC',
                    'm4a': 'AAC', 'aac': 'AAC', 'mp3': 'MP3',
                }
                print(json.dumps({
                    "type": "playlist_loaded",
                    "title": _title_early,
                    "artwork_url": _artwork_early,
                    "content_type": _infer_playlist_content_type(url, tracks),
                    "artists_string": _playlist_artists_string(tracks),
                    "release_date": _format_track_release_date(tracks),
                    "quality_badge": _quality_badge_map.get(cfg.output_format or '', ''),
                    "track_count": len(tracks),
                    "tracks": [
                        {
                            "artist": t.artist_string,
                            "title": t.title,
                            "duration_ms": t.duration_ms or 0,
                        }
                        for t in tracks
                    ],
                }), flush=True)

            tracks = service.enrich_tracks_for_download(tracks, url, options=options)

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
            _title = ""
            _artwork_summary = ""
            if tracks:
                _title = (
                    getattr(tracks[0], "playlist_name", None)
                    or getattr(tracks[0], "album", None)
                    or ""
                )
                _artwork_summary = (
                    getattr(tracks[0], "playlist_artwork_url", None)
                    or getattr(tracks[0], "artwork_url", None)
                    or ""
                )
            summary = {
                "type": "playlist_summary",
                "url": url,
                "title": _title,
                "artwork_url": _artwork_summary,
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
                "title": "",
                "artwork_url": "",
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
