import argparse
import itertools
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
import uuid
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
from antra.core.events import EngineEvent, EngineEventType
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
            # Lyrics are read back by the desktop player (GetTrackLyrics). Without
            # them here the mutagen fallback silently returns a lyrics-less probe
            # on any machine with no system ffprobe — which is every clean install,
            # since imageio_ffmpeg ships ffmpeg only.
            ("LYRICS",  ["lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS",
                         "syncedlyrics", "SYNCEDLYRICS"]),
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
        # USLT is handled separately: unlike the frames above, its .text is a
        # plain string, so v.text[0] would yield the first CHARACTER.
        for frame in tags.getall("USLT"):
            body = getattr(frame, "text", "")
            if isinstance(body, (list, tuple)):
                body = body[0] if body else ""
            if str(body).strip():
                result["LYRICS"] = str(body)
                break

    # MP4/M4A – dict-like but with Apple four-char keys
    if not result and hasattr(tags, "items"):
        mp4_map = {
            "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
            "trkn": "tracknumber", "\xa9day": "date", "\xa9lyr": "LYRICS",
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
    # issue #75 — leftover playlist files go out as their OWN top-level message
    # rather than inside the generic "event" envelope, because the Go side has to
    # intercept them: it keeps the paths and hands the webview an opaque id. A
    # filesystem path crossing the webview would come back as an arbitrary-file
    # argument to a delete call (the same rule PV-1 established for its cache).
    if event.type is EngineEventType.PLAYLIST_ORPHANS:
        print(json.dumps({
            "type": "playlist_orphans",
            "playlist": event.message or "",
            "pruned": bool(event.pruned),
            "paths": list(event.paths or []),
        }), flush=True)
        return

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
            # issue #74 - lifted out of track_data so an event carries the row
            # identity even on a path that does not ship the full metadata.
            "row_id": getattr(event.track, "row_id", None) if event.track else None,
            "track_index": event.track_index,
            "track_total": event.track_total,
            "message": event.message,
            "source": event.source,
            "error": event.error,
            "quality_label": event.quality_label,
            "tags_problem": getattr(event, "tags_problem", None),
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


# v2.0.0 PV-1 - the header the preview screen and the selective-download job
# both render. Built from the SAME helpers the mid-download playlist_loaded
# event uses, so a release cannot describe itself one way in the preview and
# another way once it is downloading.
def _playlist_header(url: str, tracks, cfg) -> dict:
    artwork = ""
    title = ""
    if tracks:
        artwork = (
            getattr(tracks[0], "playlist_artwork_url", None)
            or getattr(tracks[0], "artwork_url", None)
            or ""
        )
        title = (
            getattr(tracks[0], "playlist_name", None)
            or getattr(tracks[0], "album", None)
            or ""
        )
    return {
        "url": url,
        "title": title,
        "artwork_url": artwork,
        "content_type": _infer_playlist_content_type(url, tracks),
        "artists_string": _playlist_artists_string(tracks),
        "release_date": _format_track_release_date(tracks),
        "quality_badge": _QUALITY_BADGES.get(cfg.output_format or "", ""),
        "track_count": len(tracks),
    }


# issue #74 - a stable identity for the UI row a track owns.
#
# The app used to correlate progress events with rows by track_index, but the
# list the engine indexes is not the list the UI was given: enrichment ends in
# _stamp_disc_totals, which groups by album. A playlist holding two
# non-adjacent tracks from one album - i.e. most playlists - therefore reported
# every status against the wrong song. An id is stamped once, before the
# tracklist is emitted, and travels with the track from then on, so reordering,
# filtering or deduplication downstream cannot move a status onto another row.
#
# The prefix is per process, so ids minted by an earlier job in the same
# session - or by the preview process, which caches its tracks to disk for a
# later download process to reload - can never collide with this one's.
_ROW_ID_PREFIX = uuid.uuid4().hex[:8]
_row_id_counter = itertools.count(1)


def _stamp_row_ids(tracks) -> None:
    """Give every track a row id, leaving any it already carries alone."""
    for track in tracks or []:
        try:
            if not getattr(track, "row_id", None):
                track.row_id = f"{_ROW_ID_PREFIX}-{next(_row_id_counter)}"
        except AttributeError:
            # Not a TrackMetadata. Index keying still works; say nothing.
            return


def _verify_row_ids(tracks) -> None:
    """Keep row keying only while every track has a distinct id.

    Enrichment can rebuild track objects. If any path ever drops or duplicates
    an id, SOME rows would key by id and the rest fall back to index keying -
    a half-correct display, which is worse than the original bug because it is
    harder to notice. So the ids are cleared wholesale instead, putting the job
    back on index keying uniformly, and the reason is logged.
    """
    rows = list(tracks or [])
    if not rows:
        return
    ids = [getattr(track, "row_id", None) for track in rows]
    if all(ids) and len(set(ids)) == len(ids):
        return
    for track in rows:
        try:
            track.row_id = None
        except AttributeError:
            pass
    print(json.dumps({
        "type": "log", "level": "warning",
        "message": f"[Rows] Track identity was lost during enrichment "
                   f"({sum(1 for i in ids if not i)} of {len(ids)} missing or "
                   f"duplicated); falling back to positional track status.",
    }), flush=True)


# The rows the webview renders. Deliberately NOT the full TrackMetadata: a
# 5000-track playlist's complete metadata is tens of megabytes and the UI needs
# four fields per row. The full objects stay on disk (see --preview-out) so the
# download does not have to re-fetch the link.
def _preview_rows(tracks) -> list:
    return [
        {
            "row_id": getattr(t, "row_id", "") or "",
            "artist": t.artist_string,
            "title": t.title,
            "duration_ms": t.duration_ms or 0,
            "isrc": getattr(t, "isrc", "") or "",
            "album": getattr(t, "album", "") or "",
            "track_number": getattr(t, "track_number", None),
            "disc_number": getattr(t, "disc_number", None),
            # Only a playlist fetch sets this, so it doubles as the "is this a
            # playlist row?" flag the preview screen numbers by. It is also the
            # number the file itself gets in playlist_folder mode
            # (LibraryOrganizer.playlist_stem_for), so the preview and the
            # written filename agree.
            "playlist_position": getattr(t, "playlist_position", None),
            "artwork_url": getattr(t, "artwork_url", "") or "",
            "is_explicit": getattr(t, "is_explicit", None),
        }
        for t in tracks
    ]


_QUALITY_BADGES = {
    'lossless': 'LOSSLESS', 'flac': 'LOSSLESS',
    'alac': 'ALAC',
    'm4a': 'AAC', 'aac': 'AAC', 'mp3': 'MP3',
}


def _make_organizer(cfg):
    # Imported here, like main() does: this module has no module-level import of
    # it and no `from __future__ import annotations`, so naming it in the
    # signature would raise NameError at import time.
    from antra.utils.organizer import LibraryOrganizer
    return LibraryOrganizer(
        cfg.output_dir,
        full_albums=getattr(cfg, "library_mode", "smart_dedup") == "full_albums",
        library_index_enabled=getattr(cfg, "library_index_enabled", True),
        library_index_db_path=getattr(cfg, "library_index_db_path", ""),
        folder_structure=getattr(cfg, "folder_structure", "standard"),
        album_folder_structure=getattr(cfg, "album_folder_structure", getattr(cfg, "folder_structure", "standard")),
        playlist_folder_structure=getattr(cfg, "playlist_folder_structure", getattr(cfg, "folder_structure", "standard")),
        playlist_storage_mode=getattr(cfg, "playlist_storage_mode", "playlist_folder"),
        playlist_renumber=getattr(cfg, "playlist_renumber", True),
        playlist_prune_orphans=getattr(cfg, "playlist_prune_orphans", "ask"),
        single_track_structure=getattr(cfg, "single_track_structure", "album_numbered"),
        filename_format=getattr(cfg, "filename_format", "default"),
        single_track_filename_template=getattr(cfg, "single_track_filename_template", ""),
        album_track_filename_template=getattr(cfg, "album_track_filename_template", ""),
        folder_structure_template=getattr(cfg, "folder_structure_template", ""),
        flat_library_root=getattr(cfg, "flat_library_root", False),
        multi_disc_handling=getattr(cfg, "multi_disc_handling", "prefix"),
        track_number_padding=getattr(cfg, "track_number_padding", 2),
        illegal_character_replacement=getattr(cfg, "illegal_character_replacement", ""),
        whitespace_handling=getattr(cfg, "whitespace_handling", "preserve"),
        filename_conflict_behavior=getattr(cfg, "filename_conflict_behavior", "skip"),
        # v2.0.0 DMX-2 - output-only text transforms. They live HERE and only
        # here: json_cli used to carry four byte-identical copies of this
        # construction, and adding a kwarg to some of them is exactly how the
        # shipped app ended up ignoring every transform while the tests passed.
        title_casing=getattr(cfg, "title_casing", "nothing"),
        artist_casing=getattr(cfg, "artist_casing", "nothing"),
        remove_album_version=getattr(cfg, "remove_album_version", False),
        remove_duplicate_artists=getattr(cfg, "remove_duplicate_artists", True),
        featured_artists_mode=getattr(cfg, "featured_artists_mode", "nothing"),
        multi_artist_separator=getattr(cfg, "multi_artist_separator", "default"),
        album_various_artists=getattr(cfg, "album_various_artists", True),
    )


# v2.0.0 PV-1 - download a subset of a previewed release.
#
# The rows arrive as FULL TrackMetadata dicts saved by --preview-url, not as a
# link to re-resolve: the preview already paid the 25-35 s fetch a Qobuz album
# costs, and making the user pay it twice is exactly the regression this feature
# would otherwise introduce. Because the objects are complete, this path runs the
# same enrich -> download pipeline as a pasted link and produces the same
# playlist_summary, so history, the failed-track panel and Export all behave
# identically to a full download.
def _run_track_selection(path: str, cfg, service, options) -> None:
    _started = time.time()
    url = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error",
                          "message": f"Could not read the selected tracks: {e}"}), flush=True)
        print(json.dumps(_selection_summary(url, [], _started, error=str(e))), flush=True)
        return
    finally:
        # Written by the Go side for this one run; nothing else cleans it up.
        try:
            os.remove(path)
        except OSError:
            pass

    try:
        organizer = _make_organizer(cfg)
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error",
                          "message": f"Cannot access output directory: {e}"}), flush=True)
        print(json.dumps(_selection_summary(url, [], _started, error=str(e))), flush=True)
        return

    try:
        url = str(payload.get("url") or "")
        rows = payload.get("tracks") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("No tracks were selected.")
        tracks = [_track_from_payload(r) for r in rows]
        # issue #74 - normally a no-op: the ids were minted by --preview-url and
        # round-trip through the cache. A cache written by an older build has
        # none, so they are minted here instead.
        _stamp_row_ids(tracks)

        header = dict(payload.get("header") or {})
        # Is this a subset of the release, or all of it?
        #
        # It matters because enrich_tracks_for_download ends in
        # _stamp_disc_totals, which renumbers a group sequentially from 1 per
        # disc on the assumption that the group IS the album. For a full
        # download that is right. For a subset it is actively wrong: picking
        # tracks 5 and 9 of Scorpion wrote "1-01 - God's Plan" and
        # "2-01 - In My Feelings", mislabelling both files and colliding with
        # the real track 1 of each disc. Verified on a live download before
        # this guard existed.
        try:
            release_total = int(header.get("track_count") or 0)
        except (TypeError, ValueError):
            release_total = 0
        is_partial = release_total > 0 and len(tracks) < release_total
        # Re-derive rather than trust the stored copy: output_format can have
        # changed between the preview and pressing Download now, and the badge
        # must describe what is about to be written.
        header.update({
            "type": "playlist_loaded",
            "quality_badge": _QUALITY_BADGES.get(cfg.output_format or "", ""),
            "track_count": len(tracks),
            "tracks": _preview_rows(tracks),
        })
        header.pop("url", None)
        header.setdefault("title", "")
        header.setdefault("artwork_url", "")
        print(json.dumps({"type": "log", "level": "info",
                          "message": f"Downloading {len(tracks)} selected track(s)"
                                     f"{' from ' + url if url else ''}"}), flush=True)
        print(json.dumps(header), flush=True)

        # Enrichment still runs on a subset - it is what fills in ISRCs and album
        # data - but its renumbering is undone afterwards so each file keeps the
        # position it holds on the real release, which is also the number shown
        # in the preview. A full selection is left alone, so downloading
        # everything through the preview produces byte-identical files to
        # pasting the link.
        numbering = None
        if is_partial:
            numbering = [
                (t.track_number, t.disc_number, t.total_tracks, t.total_discs)
                for t in tracks
            ]
        tracks = service.enrich_tracks_for_download(tracks, url, options=options)
        _verify_row_ids(tracks)   # issue #74
        if numbering is not None and len(tracks) == len(numbering):
            for t, (tn, dn, tt, td) in zip(tracks, numbering):
                t.track_number, t.disc_number = tn, dn
                t.total_tracks, t.total_discs = tt, td
        elif numbering is not None:
            # Enrichment changed the list length, so position-wise restoration
            # would assign the wrong numbers. Say so rather than guess.
            print(json.dumps({"type": "log", "level": "warning",
                              "message": "Track numbering could not be preserved for this "
                                         "partial selection; files may be numbered from 1."}),
                  flush=True)
        results = service.download_tracks(
            tracks,
            options=options,
            event_callback=emit_event,
            organizer=organizer,
            # issue #75 — a strict subset of the release is NOT the whole
            # playlist; the tracks left out would all look like orphans.
            complete_playlist=not is_partial,
        )
        summary = _selection_summary(url, results, _started,
                                     title=header.get("title") or "",
                                     artwork=header.get("artwork_url") or "")
        print(json.dumps({"type": "library_update", "tracks_added": summary["downloaded"],
                          "url": url}), flush=True)
        print(json.dumps(summary), flush=True)
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error", "message": str(e)}), flush=True)
        print(json.dumps(_selection_summary(url, [], _started, error=str(e))), flush=True)


def _selection_summary(url, results, started, title="", artwork="", error=None) -> dict:
    # Imported here, like every other summary builder in this module: there is no
    # module-level `datetime` import, and naming it at module scope would be a
    # start-time NameError that py_compile cannot see.
    from datetime import datetime
    total_bytes = sum(
        os.path.getsize(r.file_path)
        for r in results
        if r.file_path and os.path.exists(r.file_path)
    )
    summary = {
        "type": "playlist_summary",
        "url": url,
        "title": title,
        "artwork_url": artwork,
        "total": len(results),
        "downloaded": sum(1 for r in results if r.status.name == "COMPLETED"),
        "failed": sum(1 for r in results if r.status.name == "FAILED"),
        "skipped": sum(1 for r in results if r.status.name == "SKIPPED"),
        "tags_incomplete": sum(1 for r in results if getattr(r, "tags_problem", None)),
        "error": error,
        "sources": {},
        "date": datetime.now().isoformat(),
        "total_mb": round(total_bytes / (1024 * 1024), 1),
        "elapsed_seconds": round(time.time() - started),
    }
    for r in results:
        if r.status.name == "COMPLETED" and r.source_used:
            summary["sources"][r.source_used] = summary["sources"].get(r.source_used, 0) + 1
    return summary


def _run_playlist_import(path: str, cfg, service, options) -> None:
    """Download an imported playlist file (v2.0.0 PORT-3).

    The rows ARE the metadata: nothing is fetched, so this works with no Spotify
    token, no sp_dc and no metadata proxy. Deliberately NOT run:
    ``enrich_tracks_for_download`` (it calls Spotify - the exposure an import
    exists to avoid) and ``_stamp_disc_totals`` (it regroups tracks by album,
    which would reorder the playlist away from the file the user imported).
    """
    started = time.time()
    source = ""
    title = ""

    def _summary(results, error=None):
        results = results or []
        total_bytes = sum(
            os.path.getsize(r.file_path) for r in results
            if r.file_path and os.path.exists(r.file_path)
        )
        sources = {}
        for r in results:
            if r.status.name == "COMPLETED" and r.source_used:
                sources[r.source_used] = sources.get(r.source_used, 0) + 1
        from datetime import datetime
        return {
            "type": "playlist_summary",
            # Not a link: history must not offer to re-queue it as one.
            "url": f"file:{source}" if source else "file:",
            "title": title,
            "artwork_url": "",
            "total": len(results),
            "downloaded": sum(1 for r in results if r.status.name == "COMPLETED"),
            "failed": sum(1 for r in results if r.status.name == "FAILED"),
            "skipped": sum(1 for r in results if r.status.name == "SKIPPED"),
            "tags_incomplete": sum(1 for r in results if getattr(r, "tags_problem", None)),
            "error": error,
            "sources": sources,
            "date": datetime.now().isoformat(),
            "total_mb": round(total_bytes / (1024 * 1024), 1),
            "elapsed_seconds": round(time.time() - started),
        }

    try:
        from antra.utils.playlist_import import load_import_payload
        try:
            data = load_import_payload(path)
        finally:
            # The rows were written to a temp file by the desktop app; nothing
            # else will clean it up.
            try:
                os.remove(path)
            except OSError:
                pass
        tracks = data["tracks"]
        source = data["source_file"]
        title = data["name"]
        isrcs = sum(1 for t in tracks if t.isrc)
        dropped = f", {data['dropped']} row(s) dropped as invalid" if data["dropped"] else ""
        print(json.dumps({
            "type": "log", "level": "info",
            "message": f"Importing playlist '{title}' from {source or 'file'} — "
                       f"{len(tracks)} tracks, {isrcs} with ISRC{dropped}",
        }), flush=True)
        if not tracks:
            raise ValueError("The imported playlist contained no usable tracks.")
        _stamp_row_ids(tracks)   # issue #74 - before the rows are emitted below

        print(json.dumps({
            "type": "playlist_loaded",
            "title": title,
            "artwork_url": "",
            "content_type": "PLAYLIST",
            "artists_string": f"Imported from {source}" if source else "Imported playlist file",
            "release_date": "",
            "quality_badge": _QUALITY_BADGES.get(cfg.output_format or '', ''),
            "track_count": len(tracks),
            "tracks": [
                {"row_id": getattr(t, "row_id", "") or "",
                 "artist": t.artist_string, "title": t.title, "duration_ms": t.duration_ms or 0,
                 "isrc": t.isrc or ""}
                for t in tracks
            ],
        }), flush=True)

        results = service.download_tracks(
            tracks,
            options=options,
            event_callback=emit_event,
            organizer=_make_organizer(cfg),
            # The imported file lists the whole playlist (issue #75).
            complete_playlist=True,
        )
        summary = _summary(results)
        print(json.dumps({"type": "library_update", "tracks_added": summary["downloaded"],
                          "url": summary["url"]}), flush=True)
        print(json.dumps(summary), flush=True)
    except Exception as e:
        print(json.dumps({"type": "log", "level": "error", "message": str(e)}), flush=True)
        print(json.dumps(_summary([], error=str(e))), flush=True)


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
                _write_config_atomic(config_path, antra_cfg, indent=2)
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


def _write_config_atomic(config_path: str, data: dict, **dump_kw) -> None:
    """Replace config.json without ever exposing a truncated file.

    open(path, "w") truncates first, so a crash or overlapping writer left an empty
    config (API keys and all settings lost). mkstemp creates the temp 0600, which is
    also the right mode for a file holding keys and cookies.
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(config_path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update_config_file(config_path: str | None, mutator) -> None:
    if not config_path or not os.path.exists(config_path):
        return
    with open(config_path, "r", encoding="utf-8") as f:
        current = json.load(f)
    mutator(current)
    _write_config_atomic(config_path, current, indent=2)


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

def _run_capture_sp_dc() -> None:
    """Open a browser, navigate to Spotify login, and poll CDP cookies until sp_dc appears."""
    import asyncio as _asyncio

    async def _runner():
        print(json.dumps({
            "type": "sp_dc_status",
            "status": "launching",
            "message": "Opening browser for Spotify login...",
        }), flush=True)

        # open.spotify.com/login now 404s ("Page not found"). Use the canonical
        # accounts.spotify.com login page, which after sign-in redirects back to
        # open.spotify.com and sets the sp_dc cookie we poll for below.
        launch_state = _launch_debug_browser_process(
            "Spotify",
            "https://accounts.spotify.com/en/login?continue=https%3A%2F%2Fopen.spotify.com%2F",
        )
        port = int(launch_state["debug_port"])

        try:
            session = await _cdp_connect(port, timeout=30.0)
            try:
                # Wait for a page tab to attach
                import requests as _req
                sid = None
                for _ in range(20):
                    try:
                        tabs = _req.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
                        page_tabs = [t for t in tabs if t.get("type") == "page"]
                        if page_tabs:
                            sid = await _cdp_attach_tab(session, page_tabs[0]["id"])
                            await session.send("Network.enable", {}, session_id=sid)
                            break
                    except Exception:
                        pass
                    await _asyncio.sleep(1)

                if not sid:
                    raise RuntimeError("Could not attach to browser tab.")

                print(json.dumps({
                    "type": "sp_dc_status",
                    "status": "waiting",
                    "message": "Sign in to Spotify in the browser. Antra will capture your session automatically.",
                }), flush=True)

                # Poll for up to 5 minutes for sp_dc cookie
                sp_dc = None
                for _ in range(300):
                    try:
                        cookies = await _cdp_get_cookies(session, sid, ["https://open.spotify.com"])
                        for c in cookies:
                            if c.get("name") == "sp_dc" and c.get("value", "").startswith("AQ"):
                                sp_dc = c["value"]
                                break
                    except Exception:
                        pass
                    if sp_dc:
                        break
                    await _asyncio.sleep(1)

                if not sp_dc:
                    raise RuntimeError("Timed out waiting for Spotify login. Please try again.")

                print(json.dumps({
                    "type": "sp_dc_captured",
                    "sp_dc": sp_dc,
                }), flush=True)

            finally:
                await session.stop()
        except Exception as exc:
            print(json.dumps({
                "type": "sp_dc_error",
                "message": str(exc),
            }), flush=True)
        finally:
            try:
                proc = launch_state.get("proc")
                if proc:
                    proc.terminate()
            except Exception:
                pass

    _asyncio.run(_runner())


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

def _run_auto_sync(cfg) -> None:
    """Download new tracks from all tracked playlists.

    Reads ``cfg.tracked_playlists`` (a list of dicts with ``url``, ``name``,
    ``last_track_ids``).  For each playlist, fetches current tracks, finds
    the diff against ``last_track_ids``, downloads new tracks only, and
    writes back the updated ``last_track_ids`` and ``last_sync_ts`` to the
    config file so subsequent runs are incremental.
    """
    import json as _json
    # tracked_playlists can't travel via env vars — read from config.json directly.
    config_path = os.environ.get("ANTRA_CONFIG_PATH", "")
    tracked = []
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as _f:
                _raw = _json.load(_f)
            tracked = _raw.get("tracked_playlists") or []
        except Exception:
            pass
    if not tracked:
        print(_json.dumps({
            "type": "auto_sync_summary",
            "message": "No tracked playlists configured.",
            "synced": 0,
            "new_tracks": 0,
        }), flush=True)
        return

    service = AntraService(cfg)
    options = RuntimeOptions(
        output_dir=cfg.output_dir,
        output_format=cfg.output_format,
        source_preference=cfg.source_preference,
    )
    total_new = 0
    synced = 0

    for entry in tracked:
        # sync_enabled defaults True for legacy entries created before the per-playlist opt-in migration
        if not entry.get("sync_enabled", True):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        name = entry.get("name") or url
        known_ids: set[str] = set(entry.get("last_track_ids") or [])

        try:
            tracks = service.fetch_playlist_tracks(url)
        except Exception as e:
            print(_json.dumps({
                "type": "auto_sync_playlist_error",
                "url": url,
                "name": name,
                "message": str(e),
            }), flush=True)
            continue

        def _track_id(t):
            if t.isrc:
                return t.isrc
            return f"{t.artist_string}:{t.title}".lower()

        current_ids = {_track_id(t): t for t in tracks}
        new_tracks = [t for tid, t in current_ids.items() if tid not in known_ids]

        print(_json.dumps({
            "type": "auto_sync_playlist_start",
            "url": url,
            "name": name,
            "total_tracks": len(tracks),
            "new_tracks": len(new_tracks),
        }), flush=True)

        if new_tracks:
            def _on_event(ev):
                print(_json.dumps({
                    "type": ev.event_type.value,
                    "track": getattr(ev, "track_title", None),
                    "status": getattr(ev, "status", None),
                }), flush=True)

            # complete_playlist stays False (issue #75): this downloads only the
            # tracks the playlist GAINED, so every track already in the folder
            # would read as an orphan. Renumbering is skipped for the same
            # reason — renaming only the new arrivals would leave the folder
            # half-numbered. A full run of the link fixes both.
            service.download_tracks(new_tracks, options=options, event_callback=_on_event)
            total_new += len(new_tracks)

        # Update entry in-place so caller can persist
        entry["last_track_ids"] = list(current_ids.keys())
        entry["last_sync_ts"] = int(time.time())
        entry["name"] = (tracks[0].playlist_name if tracks and tracks[0].playlist_name else name)
        synced += 1

    # Persist updated tracked_playlists back to config.json
    _persist_tracked_playlists(tracked)

    print(_json.dumps({
        "type": "auto_sync_summary",
        "synced": synced,
        "new_tracks": total_new,
    }), flush=True)


def _persist_tracked_playlists(tracked: list) -> None:
    """Write updated tracked_playlists back to config.json."""
    import json as _json
    config_path = os.environ.get("ANTRA_CONFIG_PATH", "")
    if not config_path or not os.path.exists(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        data["tracked_playlists"] = tracked
        _write_config_atomic(config_path, data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Could not persist tracked_playlists to config: %s", e)


def _sp_dc_from(args) -> str:
    """Read the Spotify session cookie for the early-exit subcommands.

    These branches (discovery, genres) run BEFORE the block that maps config
    settings into os.environ, so reading SPOTIFY_SP_DC from the environment
    here silently yields nothing and Discover would always fall back to Apple.
    Read the config file directly, with the env var as a fallback.
    """
    cfg_path = getattr(args, "config", "") or ""
    if cfg_path and os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return (json.load(f).get("spotify_sp_dc") or "").strip()
        except Exception:
            pass
    return (os.environ.get("SPOTIFY_SP_DC") or "").strip()


def main():

    _start_time = time.time()
    parser = argparse.ArgumentParser(description="Antra JSON backend")
    parser.add_argument("playlists", nargs="*", help="Spotify URLs to download")
    parser.add_argument("--config", help="Path to config.json from Go launcher")
    parser.add_argument("--get-fpcalc", action="store_true",
                        help="Print the path to the bundled fpcalc binary and exit")
    parser.add_argument("--get-ffmpeg-dir", action="store_true",
                        help="Print the paths to bundled ffmpeg/ffprobe (two lines) and exit")
    parser.add_argument("--debug-manifest", action="store_true",
                        help="Print endpoint-manifest cache resolution diagnostics and exit")
    parser.add_argument("--export-ffmpeg", metavar="DIR",
                        help="Copy the bundled ffmpeg/ffprobe into DIR and print their "
                             "persistent paths (two lines), then exit")
    parser.add_argument("--probe", metavar="FILE",
                        help="Run ffprobe on FILE and print JSON metadata, then exit")
    parser.add_argument("--spectrogram", metavar="FILE",
                        help="Generate spectrogram PNG for FILE and print base64 JSON, then exit")
    parser.add_argument("--discography", metavar="ARTIST_URL",
                        help="Fetch artist discography info as JSON and exit")
    parser.add_argument("--availability-url", metavar="ALBUM_URL",
                        help="Fetch Spotify/Deezer album availability intel as JSON and exit")
    parser.add_argument("--search-artists", metavar="QUERY",
                        help="Search for artists by name and return scored results as JSON, then exit")
    parser.add_argument("--search-source", default="spotify", choices=["spotify", "apple"],
                        help="Source to use for artist search (default: spotify)")
    # Issue #58 - the app could only search for artists. --search-artists is kept
    # as-is so nothing that calls it has to change; this is the wider entry point.
    parser.add_argument("--search-catalog", metavar="QUERY",
                        help="Search for songs, albums or artists and return scored results "
                             "as JSON, then exit (issue #58)")
    parser.add_argument("--search-kind", default="song", choices=["song", "album", "artist"],
                        help="What --search-catalog looks for (default: song)")
    parser.add_argument("--search-limit", type=int, default=25,
                        help="Maximum --search-catalog results (default: 25)")
    parser.add_argument("--retry-track-json", metavar="TRACK_JSON",
                        help="Retry a single previously failed track from serialized track metadata")
    parser.add_argument("--export-playlist", metavar="URL",
                        help="Resolve URL and export its tracklist to a file, then exit (v2.0.0 FR-3)")
    parser.add_argument("--export-format", default="csv",
                        choices=["csv", "m3u8", "xspf", "json", "txt"],
                        help="Export file format (default: csv)")
    parser.add_argument("--export-path", metavar="FILE", default="",
                        help="Destination file for --export-playlist")
    parser.add_argument("--discovery-json", action="store_true",
                        help="Fetch discovery data (Top Albums/Playlists) and exit")
    parser.add_argument("--discovery-region", default="us",
                        help="Storefront region for discovery data")
    parser.add_argument("--discovery-source", default="apple", choices=["apple", "spotify"],
                        help="Which service supplies discovery data")
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
    parser.add_argument("--capture-sp-dc", action="store_true",
                        help="Open a browser to Spotify login and auto-capture the sp_dc cookie")
    parser.add_argument("--spotify-library", action="store_true",
                        help="Fetch user's Spotify library (playlists + Liked Songs count) and exit")
    parser.add_argument("--apple-library", action="store_true",
                        help="Fetch user's Apple Music library (playlists + saved songs count) and exit")
    parser.add_argument("--auto-sync", action="store_true",
                        help="Run auto-sync: download new tracks from all tracked playlists, then exit")
    parser.add_argument("--preview-url", metavar="URL",
                        help="Resolve a link's metadata and tracklist WITHOUT downloading "
                             "anything (v2.0.0 PV-1). Prints one preview event.")
    parser.add_argument("--preview-out", metavar="FILE", default="",
                        help="With --preview-url: write the full TrackMetadata of every "
                             "resolved track here, so a later --tracks-json download does "
                             "not have to re-resolve the link.")
    parser.add_argument("--tracks-json", metavar="FILE",
                        help="Download the tracks in this file, written by --preview-out "
                             "and filtered to the user's selection (v2.0.0 PV-1). "
                             "The file is deleted once read.")
    parser.add_argument("--import-json", metavar="FILE",
                        help="Download a playlist file parsed by the desktop app (v2.0.0 PORT-3). "
                             "The file is deleted once read.")
    args = parser.parse_args()

    # Several subcommands (discovery, artist search, discography) exit before the
    # block further down that maps config settings into os.environ. Any of them
    # that needs the Spotify session would otherwise read an empty SPOTIFY_SP_DC
    # and silently fall back to Apple Music while looking like it worked. Publish
    # it up front so load_config() sees it on every path.
    if not (os.environ.get("SPOTIFY_SP_DC") or "").strip():
        _early_sp_dc = _sp_dc_from(args)
        if _early_sp_dc:
            os.environ["SPOTIFY_SP_DC"] = _early_sp_dc

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
            elif "open.spotify.com" in url and "/artist/" in url:
                # Partner GraphQL first: the public API 429s on throttled networks,
                # which would otherwise surface as an empty or failed discography
                # for an artist Spotify plainly has. Falls through to the public
                # client below only if this path fails.
                from antra.core.config import load_config
                cfg = load_config()
                sp_dc = (getattr(cfg, "spotify_sp_dc", "") or "").strip() or _sp_dc_from(args)
                info = None
                if sp_dc:
                    try:
                        from antra.core.spotify_partner import SpotifyPartnerClient
                        info = SpotifyPartnerClient(sp_dc).fetch_artist_discography_info(url)
                    except Exception:
                        info = None
                if not info or not info.get("albums"):
                    from antra.core.spotify import SpotifyClient
                    sp = SpotifyClient(
                        cfg.spotify_client_id,
                        cfg.spotify_client_secret,
                        cfg.spotify_market,
                        redirect_uri=cfg.spotify_redirect_uri,
                        auth_storage_path=cfg.spotify_auth_path,
                    )
                    info = sp.fetch_artist_discography_info(url)
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

    if args.availability_url:
        try:
            from antra.core.config import load_config
            from antra.core.availability import lookup_album_availability
            cfg = load_config()
            info = lookup_album_availability(args.availability_url, cfg)
            print(json.dumps({"type": "availability", "data": info}), flush=True)
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

    if args.search_catalog:
        try:
            from antra.core.config import load_config
            cfg = load_config()
            service = AntraService(config=cfg)
            results = service.search_catalog(
                args.search_catalog,
                kind=args.search_kind,
                source=args.search_source,
                limit=args.search_limit,
            )
            print(json.dumps({"type": "catalog_search", "data": results}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.discovery_json:
        try:
            from antra.core.discovery import AppleDiscovery
            data = None
            source = "apple"
            if args.discovery_source == "spotify":
                sp_dc = _sp_dc_from(args)
                if sp_dc:
                    from antra.core.discovery import SpotifyDiscovery
                    data = SpotifyDiscovery(sp_dc).get_discovery_data(
                        storefront=args.discovery_region
                    )
                    # The home feed is personalised and unavailable logged-out;
                    # an empty result also covers a rotated persisted-query hash.
                    # Either way fall back rather than show an empty tab.
                    if data.get("top_albums") or data.get("top_playlists"):
                        source = "spotify"
                    else:
                        data = None
            if data is None:
                data = AppleDiscovery().get_discovery_data(
                    storefront=args.discovery_region,
                    genre_id=args.discovery_genre_id if args.discovery_genre_id else None,
                    genre_name=args.discovery_genre_name if args.discovery_genre_name else None
                )
            print(json.dumps({"type": "discovery", "data": data, "source": source}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.discovery_genres_only:
        try:
            # Spotify's home feed has no genre facet, so it reports no genres and
            # the UI hides the picker. Apple keeps its chart genres.
            if args.discovery_source == "spotify" and _sp_dc_from(args):
                from antra.core.discovery import SpotifyDiscovery
                genres = SpotifyDiscovery(_sp_dc_from(args)).get_genres()
            else:
                from antra.core.discovery import AppleDiscovery
                genres = AppleDiscovery().get_genres(storefront=args.discovery_region)
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

    if args.debug_manifest:
        import sys as _sys
        from antra.core import endpoint_manifest as _em
        info = {
            "frozen": bool(getattr(_sys, "frozen", False)),
            "meipass": getattr(_sys, "_MEIPASS", None),
            "module_file": getattr(_em, "__file__", None),
            "data_dir": str(_em._DATA_DIR),
            "cache_path": str(_em._CACHE_PATH),
            "cache_exists": _em._CACHE_PATH.exists(),
            "env_localappdata": os.environ.get("LOCALAPPDATA"),
            "manifest_fetch_disabled": _em.manifest_fetch_disabled(),
        }
        try:
            import platformdirs as _pd
            info["platformdirs_ok"] = True
            info["platformdirs_file"] = getattr(_pd, "__file__", None)
            info["platformdirs_result"] = _pd.user_data_dir("Antra", "Antra")
        except Exception as _e:
            info["platformdirs_ok"] = False
            info["platformdirs_error"] = repr(_e)
        m = _em.load_endpoint_manifest()
        info["resolved_tidal"] = m.mirror_tidal
        info["resolved_qobuz"] = m.mirror_qobuz
        print(json.dumps(info, indent=2))
        sys.exit(0)

    if args.get_fpcalc:

        from antra.utils.runtime import get_fpcalc_exe

        print(get_fpcalc_exe() or '')

        return 0


    if args.get_ffmpeg_dir:
        from antra.utils.runtime import get_ffmpeg_exe, get_ffprobe_exe
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
        # Output two lines: ffmpeg full path, ffprobe full path (empty if not found)
        # NOTE: inside the PyInstaller bundle these may point into _MEIPASS, which
        # is deleted when this process exits. Callers that need a path they can
        # exec LATER must use --export-ffmpeg instead.
        print(ffmpeg or "")
        print(ffprobe or "")
        sys.exit(0)

    if args.export_ffmpeg:
        from antra.utils.runtime import export_runtime_binaries
        ffmpeg, ffprobe = export_runtime_binaries(args.export_ffmpeg)
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
                _fmt = settings["output_format"]
                # v1.1.8 FEAT-1: resolve the unified "atmos" choice to a concrete
                # atmos-<service> using the pasted URL, since the Atmos mix is
                # service-specific. Done here because this is the first point
                # where BOTH the chosen format and the URL are known. A link from
                # a non-Atmos service fails loudly rather than silently
                # downloading a stereo copy.
                if str(_fmt).lower() == "atmos":
                    from antra.core.service import detect_atmos_service
                    _urls = [u for u in (getattr(args, "playlists", None) or []) if u]
                    _services = {detect_atmos_service(u) for u in _urls}
                    if not _urls or None in _services or not _services:
                        print(json.dumps({"type": "error", "message":
                            "Dolby Atmos is only available from Tidal, Apple Music or Amazon "
                            "Music. Paste a link from one of those services to download an "
                            "Atmos mix."}), flush=True)
                        sys.exit(1)
                    if len(_services) > 1:
                        # One run has one output format, and the Atmos mix is
                        # service-specific — mixing services cannot be honoured.
                        print(json.dumps({"type": "error", "message":
                            "Dolby Atmos downloads can only cover one service at a time. "
                            "Paste links from a single service (Tidal, Apple Music or "
                            "Amazon Music) per download."}), flush=True)
                        sys.exit(1)
                    _fmt = f"atmos-{_services.pop()}"
                os.environ["OUTPUT_FORMAT"] = _fmt
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
            # issue #78. Unconditional with its documented default, per the
            # BUG-23 trap noted further down: a key absent from an older
            # config.json must not let a stale .env value decide the layout.
            os.environ["PLAYLIST_STORAGE_MODE"] = settings.get("playlist_storage_mode") or "playlist_folder"
            # issue #75 — same unconditional rule. `or True` would be wrong here:
            # False is a value the user chose, and `or` cannot tell it from an
            # absent key, so the toggle could never be turned off.
            _renumber = settings.get("playlist_renumber")
            os.environ["PLAYLIST_RENUMBER"] = "true" if (_renumber is None or _renumber) else "false"
            os.environ["PLAYLIST_PRUNE_ORPHANS"] = settings.get("playlist_prune_orphans") or "ask"
            os.environ["SINGLE_TRACK_STRUCTURE"] = settings.get("single_track_structure") or "album_numbered"
            os.environ["FILENAME_FORMAT"] = settings.get("filename_format") or "default"
            os.environ["SINGLE_TRACK_FILENAME_TEMPLATE"] = settings.get("single_track_filename_template") or ""
            os.environ["ALBUM_ZIP_NAME_TEMPLATE"] = settings.get("album_zip_name_template") or ""
            os.environ["ALBUM_TRACK_FILENAME_TEMPLATE"] = settings.get("album_track_filename_template") or ""
            # v1.1.8 FEAT-11 — tri-state. The Go side sends the key only when the
            # user has touched it (*string), so: absent/None = never configured
            # (use the default), "" = deliberately cleared (flat library root),
            # anything else = a real template. Set unconditionally so a stale
            # .env value can never win (the BUG-23 trap).
            _fst = settings.get("folder_structure_template")
            os.environ["FOLDER_STRUCTURE_TEMPLATE"] = _fst or ""
            os.environ["FLAT_LIBRARY_ROOT"] = "true" if _fst == "" else "false"
            os.environ["MULTI_DISC_HANDLING"] = settings.get("multi_disc_handling") or "prefix"
            os.environ["TRACK_NUMBER_PADDING"] = str(settings.get("track_number_padding") or 2)
            illegal_character_replacement = settings.get("illegal_character_replacement")
            os.environ["ILLEGAL_CHARACTER_REPLACEMENT"] = "" if illegal_character_replacement is None else str(illegal_character_replacement)
            os.environ["WHITESPACE_HANDLING"] = settings.get("whitespace_handling") or "preserve"
            os.environ["FILENAME_CONFLICT_BEHAVIOR"] = settings.get("filename_conflict_behavior") or "skip"
            os.environ["FETCH_LYRICS"] = "true" if settings.get("fetch_lyrics", True) else "false"

            # Unconditional, with their documented defaults, for the BUG-23
            # reason spelled out below: a key absent from an older config.json
            # must not leave the env var unset and let a stale .env value win.
            os.environ["PREFER_EXPLICIT"] = "true" if settings.get("prefer_explicit", True) else "false"
            os.environ["STRICT_MATCHING"] = "true" if settings.get("strict_matching", False) else "false"
            # v1.1.8 FEAT-2/3/4. Set unconditionally with their documented
            # defaults: an absent key in an older config.json must not leave the
            # env var unset and let a stale .env value win — that is exactly the
            # trap that caused BUG-23 for the filename-format setting.
            os.environ["STRICT_FORMAT"] = "true" if settings.get("strict_format", True) else "false"
            os.environ["PREVENT_LOSSY_TRANSCODE"] = (
                "true" if settings.get("prevent_lossy_transcode", True) else "false"
            )
            os.environ["WRITE_ANTRA_TAGS"] = (
                "true" if settings.get("write_antra_tags", True) else "false"
            )

            # API key for self-hosted mirror servers
            if settings.get("antra_api_key") is not None:
                os.environ["ANTRA_API_KEY"] = str(settings.get("antra_api_key") or "")

            # Download source override. "auto" preserves the full quality-aware
            # resolver chain; a list limits downloads to the selected services.
            download_sources = settings.get("download_sources")
            if isinstance(download_sources, list):
                cleaned_sources = [str(src).strip() for src in download_sources if str(src).strip()]
                os.environ["SOURCE_PREFERENCES"] = ",".join(cleaned_sources) if cleaned_sources else "auto"
            elif "download_source" in settings:
                os.environ["SOURCE_PREFERENCES"] = str(settings.get("download_source") or "auto")

            if "save_cover_art_sidecar" in settings:
                os.environ["SAVE_COVER_ART_SIDECAR"] = "true" if settings["save_cover_art_sidecar"] else "false"

            # v2.0.0 FR-2 - cover art settings. Set UNCONDITIONALLY with their
            # defaults: the `if key in settings` form above is the BUG-23 trap,
            # where a stale .env value wins for any key an older config.json
            # does not carry.
            def _art_str(key: str, default: str) -> str:
                value = settings.get(key)
                value = "" if value is None else str(value).strip()
                return value or default

            def _art_int(key: str, default: int) -> str:
                try:
                    return str(int(settings.get(key)))
                except (TypeError, ValueError):
                    return str(default)

            def _art_bool(key: str, default: bool) -> str:
                value = settings.get(key)
                if value is None:
                    value = default
                return "true" if value else "false"

            os.environ["COVER_IMAGE_TEMPLATE"] = _art_str("cover_image_template", "cover")
            os.environ["LOCAL_ARTWORK_SIZE"] = _art_int("local_artwork_size", 0)
            os.environ["LOCAL_ARTWORK_FORMAT"] = _art_str("local_artwork_format", "jpg")
            os.environ["JPEG_IMAGE_QUALITY"] = _art_int("jpeg_image_quality", 90)
            os.environ["EMBEDDED_ARTWORK_SIZE"] = _art_int("embedded_artwork_size", 1000)
            os.environ["EMBEDDED_ARTWORK_PNG"] = _art_bool("embedded_artwork_png", False)
            os.environ["SAVE_ARTIST_IMAGE"] = _art_bool("save_artist_image", False)
            os.environ["ARTIST_IMAGE_TEMPLATE"] = _art_str("artist_image_template", "folder")

            # v2.0.0 PORT-2 - sidecar files. Set UNCONDITIONALLY with their
            # defaults for the same BUG-23 reason as the FR-2 block above: the
            # `if key in settings` form lets a stale .env win for any key an
            # older config.json does not carry.
            os.environ["SIDECAR_CUE"] = _art_bool("sidecar_cue", False)
            os.environ["SIDECAR_NFO"] = _art_bool("sidecar_nfo", False)
            os.environ["SIDECAR_JSON"] = _art_bool("sidecar_json", False)

            # v2.0.0 DMX-1 - per-tag write toggles. Set UNCONDITIONALLY, same
            # BUG-23 reasoning as the two blocks above: an older config.json
            # carries none of these keys, and leaving the env var unset would
            # let a stale .env value decide which tags get written.
            _dis = settings.get("disabled_tags")
            if isinstance(_dis, list):
                _dis_clean = [str(name).strip().lower() for name in _dis if str(name).strip()]
            else:
                _dis_clean = []
            os.environ["DISABLED_TAGS"] = ",".join(_dis_clean)
            os.environ["DATE_FORMAT"] = _art_str("date_format", "iso")
            os.environ["WRITE_ID3V1"] = _art_bool("write_id3v1", True)

            # v2.0.0 DMX-2 - output-only text transforms. Set UNCONDITIONALLY
            # with their defaults, same BUG-23 reasoning as every block above:
            # an older config.json carries none of these keys, and leaving the
            # env var unset would let a stale .env value rewrite the user's
            # titles and artist tags.
            os.environ["TITLE_CASING"] = _art_str("title_casing", "nothing")
            os.environ["ARTIST_CASING"] = _art_str("artist_casing", "nothing")
            os.environ["REMOVE_ALBUM_VERSION"] = _art_bool("remove_album_version", False)
            os.environ["REMOVE_DUPLICATE_ARTISTS"] = _art_bool("remove_duplicate_artists", True)
            os.environ["FEATURED_ARTISTS_MODE"] = _art_str("featured_artists_mode", "nothing")
            os.environ["MULTI_ARTIST_SEPARATOR"] = _art_str("multi_artist_separator", "default")
            os.environ["ALBUM_VARIOUS_ARTISTS"] = _art_bool("album_various_artists", True)

            # v2.0.0 DMX-6 - post-download command hook. Set
            # UNCONDITIONALLY with their defaults, same BUG-23 reasoning as
            # every block above - and it matters most here: leaving these
            # unset would let a stale .env decide whether a program runs.
            os.environ["POST_COMMAND_ENABLED"] = _art_bool("post_command_enabled", False)
            os.environ["POST_COMMAND_CONFIRMED"] = _art_bool("post_command_confirmed", False)
            os.environ["POST_COMMAND"] = _art_str("post_command", "")
            os.environ["POST_COMMAND_MODE"] = _art_str("post_command_mode", "track")
            os.environ["POST_COMMAND_TIMEOUT"] = _art_int("post_command_timeout", 30)

            # v2.0.0 SF-2 - ReplayGain. Unconditional, same BUG-23 reasoning:
            # a stale .env must not switch a decode pass per track on.
            os.environ["REPLAYGAIN_ENABLED"] = _art_bool("replaygain_enabled", False)

            # Audio fingerprint verification. Unconditional for the same
            # BUG-23 reason: a stale .env must not switch a per-track
            # preview fetch on behind the user's back.
            os.environ["VERIFY_AUDIO_ENABLED"] = _art_bool("verify_audio_enabled", True)

            # Auto-sync settings
            if "auto_sync_enabled" in settings:
                os.environ["AUTO_SYNC_ENABLED"] = "true" if settings["auto_sync_enabled"] else "false"
            if "auto_sync_hour" in settings:
                os.environ["AUTO_SYNC_HOUR"] = str(settings.get("auto_sync_hour") or 6)
            if "auto_sync_minute" in settings:
                os.environ["AUTO_SYNC_MINUTE"] = str(settings.get("auto_sync_minute") or 0)
            if "auto_sync_days" in settings:
                os.environ["AUTO_SYNC_DAYS"] = str(settings.get("auto_sync_days") or 127)

            # Store config file path so _persist_tracked_playlists can write back
            os.environ["ANTRA_CONFIG_PATH"] = args.config

        except Exception as e:
            print(json.dumps({"type": "log", "level": "error", "message": f"Failed to load config: {e}"}))

    # Fall back to "auto" if download_source was not set by the config block above.
    os.environ.setdefault("SOURCE_PREFERENCE", "auto")

    from antra.core.config import load_config
    from antra.utils.organizer import LibraryOrganizer
    cfg = load_config()

    if args.tidal_validate:
        print(json.dumps(_validate_tidal_auth(cfg)), flush=True)
        sys.exit(0)

    if args.tidal_oauth_login:
        _run_tidal_oauth_login(cfg, args.config)
        sys.exit(0)

    if args.capture_sp_dc:
        _run_capture_sp_dc()
        sys.exit(0)

    if args.apple_browser_login:
        _run_apple_browser_login(cfg, args.config)
        sys.exit(0)

    if args.amazon_browser_login:
        _run_amazon_browser_login(cfg, args.config)
        sys.exit(0)

    if args.spotify_library:
        try:
            sp_dc = cfg.spotify_sp_dc
            if not sp_dc:
                raise RuntimeError(
                    "No Spotify sp_dc cookie configured. "
                    "Add it in Settings → Spotify Podcasts to enable library access."
                )
            from antra.core.spotify_library import SpotifyLibraryClient
            client = SpotifyLibraryClient(sp_dc)
            data = client.get_library()
            print(json.dumps({"type": "spotify_library", "data": data}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.apple_library:
        try:
            authorization = (cfg.apple_authorization_token or "").strip()
            music_user_token = (cfg.apple_music_user_token or "").strip()
            storefront = (cfg.apple_storefront or "us").strip() or "us"
            if not authorization or not music_user_token:
                raise RuntimeError(
                    "No Apple Music web session configured. "
                    "Connect Apple Music in Settings to enable library access."
                )
            from antra.core.apple_library import AppleLibraryClient
            client = AppleLibraryClient(
                authorization_token=authorization,
                music_user_token=music_user_token,
                storefront=storefront,
            )
            data = client.get_library()
            print(json.dumps({"type": "apple_library", "data": data}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    if args.auto_sync:
        try:
            _run_auto_sync(cfg)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
        sys.exit(0)

    print(json.dumps({"type": "log", "level": "info", "message": f"[Config] Filename format: {cfg.filename_format} | Album layout: {getattr(cfg, 'album_folder_structure', cfg.folder_structure)} | Playlist layout: {getattr(cfg, 'playlist_folder_structure', cfg.folder_structure)} | Single layout: {getattr(cfg, 'single_track_structure', 'album_numbered')} | Output format: {cfg.output_format}"}))

    service = AntraService(cfg)
    options = RuntimeOptions(
        output_dir=cfg.output_dir,
        source_preference=cfg.source_preference,
        output_format=cfg.output_format
    )

    # v2.0.0 PV-1 - resolve the link and stop. Nothing is written to the library
    # and no organizer is built, so a preview can never touch the user's files.
    if args.preview_url:
        try:
            # A podcast link never goes through fetch_playlist_tracks - the
            # download path routes it to _download_podcast_url before that call -
            # so it has nothing to preview. Say so in a way the frontend can act
            # on: it falls back to queueing the link directly, which is what
            # podcasts did before the preview step existed.
            from antra.core.podcast import is_podcast_url
            if is_podcast_url(args.preview_url):
                print(json.dumps({
                    "type": "preview_unsupported",
                    "reason": "Podcast episodes and shows are downloaded without a preview.",
                }), flush=True)
                sys.exit(0)
            tracks = service.fetch_playlist_tracks(
                args.preview_url, options=options, enrich_override=False
            )
            if not tracks:
                raise RuntimeError("No tracks were found at that link.")
            # issue #74 - stamped before _preview_rows renders them and before
            # the cache below is written, so the download process that reloads
            # that cache addresses the same rows the preview screen created.
            _stamp_row_ids(tracks)
            data = _playlist_header(args.preview_url, tracks, cfg)
            data["tracks"] = _preview_rows(tracks)
            if args.preview_out:
                # Full objects for the download that may follow. Written before
                # the event is printed so the frontend never holds a preview id
                # whose cache does not exist yet.
                with open(args.preview_out, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "url": args.preview_url,
                            "header": _playlist_header(args.preview_url, tracks, cfg),
                            "tracks": [asdict(t) for t in tracks],
                        },
                        fh,
                    )
            print(json.dumps({"type": "preview", "data": data}), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
            sys.exit(1)
        sys.exit(0)

    # v2.0.0 FR-3 - export the resolved tracklist without downloading anything.
    if args.export_playlist:
        try:
            from antra.utils.playlist_export import (
                EXPORT_EXTENSIONS,
                export_playlist,
                suggested_filename,
            )
            from antra_shared.filename_prefs import migrate_legacy_templates
            from antra.utils.transcoder import OUTPUT_FORMAT_EXTENSION

            tracks = service.fetch_playlist_tracks(
                args.export_playlist, options=options, enrich_override=False
            )
            if not tracks:
                raise RuntimeError("No tracks were found at that link.")

            prefs = migrate_legacy_templates(
                {
                    "single_track_filename_template": getattr(cfg, "single_track_filename_template", ""),
                    "album_track_filename_template": getattr(cfg, "album_track_filename_template", ""),
                    "folder_structure_template": getattr(cfg, "folder_structure_template", ""),
                    "multi_disc_handling": getattr(cfg, "multi_disc_handling", "prefix"),
                    "track_number_padding": getattr(cfg, "track_number_padding", 2),
                    "illegal_character_replacement": getattr(cfg, "illegal_character_replacement", "_"),
                    "whitespace_handling": getattr(cfg, "whitespace_handling", "preserve"),
                    "filename_conflict_behavior": getattr(cfg, "filename_conflict_behavior", "skip"),
                    # v2.0.0 DMX-2 - without these the exported playlist would
                    # render paths from untransformed text and point at files
                    # that are not on disk under those names.
                    "title_casing": getattr(cfg, "title_casing", "nothing"),
                    "artist_casing": getattr(cfg, "artist_casing", "nothing"),
                    "remove_album_version": getattr(cfg, "remove_album_version", False),
                    "remove_duplicate_artists": getattr(cfg, "remove_duplicate_artists", True),
                    "featured_artists_mode": getattr(cfg, "featured_artists_mode", "nothing"),
                    "multi_artist_separator": getattr(cfg, "multi_artist_separator", "default"),
                    "album_various_artists": getattr(cfg, "album_various_artists", True),
                },
                filename_format=getattr(cfg, "filename_format", "default"),
                album_folder_structure=getattr(cfg, "album_folder_structure", getattr(cfg, "folder_structure", "standard")),
            )

            title = ""
            for candidate in tracks:
                title = candidate.playlist_name or candidate.album or ""
                if title:
                    break

            out_path = args.export_path or suggested_filename(title, args.export_format, prefs)
            audio_ext = OUTPUT_FORMAT_EXTENSION.get(cfg.output_format or "", ".flac") or ".flac"

            summary = export_playlist(
                tracks,
                args.export_format,
                out_path,
                playlist_title=title,
                source_url=args.export_playlist,
                filename_preferences=prefs,
                audio_extension=audio_ext,
            )
            summary["type"] = "playlist_exported"
            print(json.dumps(summary), flush=True)
        except Exception as e:
            print(json.dumps({"type": "error", "message": str(e)}), flush=True)
            sys.exit(1)
        sys.exit(0)

    if args.retry_track_json:
        try:
            organizer = _make_organizer(cfg)
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

    if args.import_json:
        _run_playlist_import(args.import_json, cfg, service, options)
        print(json.dumps({"type": "done"}), flush=True)
        return

    if args.tracks_json:
        _run_track_selection(args.tracks_json, cfg, service, options)
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
        organizer = _make_organizer(cfg)
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
            # Progressive page callback — fires after each 1000-track GraphQL page.
            # First call emits playlist_loaded (partial), subsequent calls emit tracks_appended.
            _page_state = {"sent": 0}

            def _on_page(tracks_so_far):
                new_tracks = tracks_so_far[_page_state["sent"]:]
                if not new_tracks:
                    return
                # issue #74 - before the rows reach the webview, never after.
                _stamp_row_ids(new_tracks)
                if _page_state["sent"] == 0:
                    _artwork_p = (
                        getattr(tracks_so_far[0], "playlist_artwork_url", None)
                        or getattr(tracks_so_far[0], "artwork_url", None)
                        or ""
                    )
                    _title_p = (
                        getattr(tracks_so_far[0], "playlist_name", None)
                        or getattr(tracks_so_far[0], "album", None)
                        or ""
                    )
                    print(json.dumps({
                        "type": "playlist_loaded",
                        "partial": True,
                        "title": _title_p,
                        "artwork_url": _artwork_p,
                        "content_type": _infer_playlist_content_type(url, tracks_so_far),
                        "artists_string": _playlist_artists_string(tracks_so_far),
                        "release_date": _format_track_release_date(tracks_so_far),
                        "quality_badge": _QUALITY_BADGES.get(cfg.output_format or '', ''),
                        "track_count": len(tracks_so_far),
                        "tracks": [
                            {"row_id": getattr(t, "row_id", "") or "",
                             "artist": t.artist_string, "title": t.title, "duration_ms": t.duration_ms or 0,
                             "isrc": getattr(t, "isrc", "") or ""}
                            for t in tracks_so_far
                        ],
                    }), flush=True)
                else:
                    print(json.dumps({
                        "type": "tracks_appended",
                        "tracks": [
                            {"row_id": getattr(t, "row_id", "") or "",
                             "artist": t.artist_string, "title": t.title, "duration_ms": t.duration_ms or 0,
                             "isrc": getattr(t, "isrc", "") or ""}
                            for t in new_tracks
                        ],
                    }), flush=True)
                _page_state["sent"] = len(tracks_so_far)

            tracks = service.fetch_playlist_tracks(url, options=options, enrich_override=False, page_callback=_on_page)
            # issue #74 - idempotent: the page callback above has already
            # stamped anything it emitted.
            _stamp_row_ids(tracks)

            # Emit the full tracklist immediately after metadata is fetched,
            # before any individual downloads begin.
            # Skip if progressive callback already sent everything.
            if tracks and _page_state["sent"] == 0:
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
                print(json.dumps({
                    "type": "playlist_loaded",
                    "title": _title_early,
                    "artwork_url": _artwork_early,
                    "content_type": _infer_playlist_content_type(url, tracks),
                    "artists_string": _playlist_artists_string(tracks),
                    "release_date": _format_track_release_date(tracks),
                    "quality_badge": _QUALITY_BADGES.get(cfg.output_format or '', ''),
                    "track_count": len(tracks),
                    "tracks": [
                        {
                            "row_id": getattr(t, "row_id", "") or "",
                            "artist": t.artist_string,
                            "title": t.title,
                            "duration_ms": t.duration_ms or 0,
                            "isrc": getattr(t, "isrc", "") or "",
                        }
                        for t in tracks
                    ],
                }), flush=True)

            tracks = service.enrich_tracks_for_download(tracks, url, options=options)
            # issue #74 - enrichment is the step that reorders the list and can
            # rebuild its objects. This is the point where the ids have to still
            # be intact, so it is the point where they are checked.
            _verify_row_ids(tracks)

            results = service.download_tracks(
                tracks,
                options=options,
                event_callback=emit_event,
                organizer=organizer,
                # The link's full tracklist was just fetched (issue #75).
                complete_playlist=True,
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
                "tags_incomplete": sum(1 for r in results if getattr(r, "tags_problem", None)),
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
