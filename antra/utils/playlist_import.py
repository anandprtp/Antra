"""
v2.0.0 PORT-3 - turn imported playlist-file rows into downloadable tracks.

The file is parsed in the frontend (antra-wails/frontend/src/lib/importer.ts);
Go writes the parsed rows to a temp JSON file and passes its path to
``json_cli --import-json``. This module is the Python half: it validates every
row again and builds TrackMetadata.

Two things are deliberate:

* **No matching happens here.** Antra's resolver is ISRC-first at download
  time, so a row IS the metadata. A matching pass at import time would duplicate
  that work and add the upstream rate-limit exposure the feature exists to avoid.

* **Rows are validated again, not trusted.** The frontend is ours, but the
  values came from an arbitrary file on disk and flow into filenames. Bounds
  match Antra-Web's ``ImportedTrack`` model. Where the web server rejects the
  whole request with a 422, the desktop drops only the offending row (or just
  the offending optional field) and COUNTS it, because one malformed row in a
  2,000-track export should not cost the other 1,999.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from antra.core.models import TrackMetadata

MAX_TRACKS = 5000
MAX_TITLE = 500
MAX_ARTISTS = 12
MAX_ARTIST = 200
MAX_NAME = 200
DEFAULT_NAME = "Imported playlist"

_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")
# Control characters would reach filenames and tags; the organizer sanitises
# path separators, but a newline in a title is worth removing at the source.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_RE.sub(" ", value).strip()[:limit].strip()


def _bounded_int(value: Any, low: int, high: int) -> Optional[int]:
    # bool is an int subclass - "true" in a JSON cell must not become track 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def normalize_isrc(value: Any) -> Optional[str]:
    """A malformed ISRC is worse than none: the resolver matches ISRC-first, so a
    bad one can pin the wrong recording. It is dropped, not rejected - the row is
    still downloadable by title and artist."""
    if not isinstance(value, str) or not value:
        return None
    s = value.upper().replace("-", "").replace(" ", "")
    return s if _ISRC_RE.match(s) else None


def sanitize_row(row: Any) -> Optional[dict]:
    """One row, bounded. Returns None when the row cannot be downloaded."""
    if not isinstance(row, dict):
        return None
    title = _text(row.get("title"), MAX_TITLE)
    raw_artists = row.get("artists")
    if not isinstance(raw_artists, list):
        return None
    artists = [a for a in (_text(x, MAX_ARTIST) for x in raw_artists) if a][:MAX_ARTISTS]
    if not title or not artists:
        return None
    return {
        "title": title,
        "artists": artists,
        "album": _text(row.get("album"), MAX_TITLE),
        "isrc": normalize_isrc(row.get("isrc")),
        "duration_ms": _bounded_int(row.get("duration_ms"), 1, 86_400_000),
        "track_number": _bounded_int(row.get("track_number"), 1, 999),
        "disc_number": _bounded_int(row.get("disc_number"), 1, 99),
        "release_year": _bounded_int(row.get("release_year"), 1900, 2200),
    }


def sanitize_rows(rows: Any) -> tuple[list[dict], int]:
    """(usable rows, number dropped). Rows beyond MAX_TRACKS count as dropped."""
    if not isinstance(rows, list):
        return [], 0
    clean: list[dict] = []
    dropped = 0
    for row in rows:
        if len(clean) >= MAX_TRACKS:
            dropped += 1
            continue
        ok = sanitize_row(row)
        if ok is None:
            dropped += 1
        else:
            clean.append(ok)
    return clean, dropped


def playlist_name(value: Any) -> str:
    return _text(value, MAX_NAME) or DEFAULT_NAME


def tracks_from_rows(rows: list[dict], name: str) -> list[TrackMetadata]:
    """Build TrackMetadata in file order.

    ``playlist_position`` is load-bearing: without it the organizer falls back to
    ``track_number``, which most imports lack, and every file lands as
    ``01 - ...`` in one folder - the v1.1.7 BUG-1/BUG-3 defect.
    """
    out = []
    for i, row in enumerate(rows):
        out.append(TrackMetadata(
            title=row["title"],
            artists=list(row["artists"]),
            album=row.get("album") or "",
            request_kind="playlist",
            playlist_name=name,
            playlist_position=i + 1,
            track_number=row.get("track_number"),
            disc_number=row.get("disc_number"),
            duration_ms=row.get("duration_ms"),
            isrc=row.get("isrc"),
            release_year=row.get("release_year"),
        ))
    return out


def load_import_payload(path: str) -> dict:
    """Read the JSON Go wrote and return a validated import.

    Keys: ``name``, ``source_file``, ``format``, ``tracks`` (TrackMetadata),
    ``dropped``. Raises ValueError with a user-facing message on a bad payload.
    """
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"The imported playlist data is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The imported playlist data has the wrong shape.")
    rows, dropped = sanitize_rows(data.get("tracks"))
    name = playlist_name(data.get("name"))
    return {
        "name": name,
        "source_file": _text(data.get("source_file"), MAX_NAME),
        "format": _text(data.get("format"), 16),
        "tracks": tracks_from_rows(rows, name),
        "dropped": dropped,
    }
