"""Export a resolved tracklist to CSV / M3U8 / XSPF / JSON / TXT (v2.0.0 FR-3).

Design notes worth keeping:

* **Columns are chosen from the DATA, not declared up front.** Coverage is
  source-dependent and was measured, not assumed: an Apple Music album fills
  genres 14/14 while a Qobuz album fills them 0/13, and `label` is empty at
  resolve time for both because `metadata_enricher` fills it during DOWNLOAD.
  So a column that is empty for every track in *this* export is omitted, which
  is the spec's own rule - a header row of blanks looks like data loss.

* **The CSV uses Exportify's exact header names**, so it round-trips into
  Antra-Web's importer (`HEADER_MAPPINGS` there accepts "Track Name",
  "Artist Name(s)", "Album Name", "ISRC", "Duration (ms)", ...). "Artist
  Name(s)" is emitted BEFORE "Album Artist Name(s)" on purpose: that importer
  documents "first header wins" and both map to its artist field.

* **M3U8/XSPF locations are the EXPECTED library path**, rendered from the
  user's own filename preferences, not a service URL. Measured: a Qobuz album
  carries no per-track identifier of any kind (0/13 for every id field), so a
  URL-based locator would be empty for most exports. The rendered path is
  available for every track and resolves once the release is downloaded.
"""
from __future__ import annotations

import csv
import json
import os
from typing import Any, Optional
from xml.sax.saxutils import escape as _xml_escape

from antra_shared.filename_prefs import (
    build_folder_path,
    build_single_track_stem,
    build_track_stem,
    sanitize_filename_segment,
)

EXPORT_FORMATS = ("csv", "m3u8", "xspf", "json", "txt")

EXPORT_EXTENSIONS = {
    "csv": ".csv",
    "m3u8": ".m3u8",
    "xspf": ".xspf",
    "json": ".json",
    "txt": ".txt",
}

# Exportify column order. Track artist before album artist - see module docstring.
_CSV_COLUMNS: list[tuple[str, str]] = [
    ("Track URI", "uri"),
    ("Track Name", "title"),
    ("Album Name", "album"),
    ("Artist Name(s)", "artists"),
    ("Album Artist Name(s)", "album_artists"),
    ("Release Date", "release_date"),
    ("Duration (ms)", "duration_ms"),
    ("Explicit", "explicit"),
    ("ISRC", "isrc"),
    ("Track Number", "track_number"),
    ("Disc Number", "disc_number"),
    ("Genres", "genres"),
    ("Record Label", "label"),
    ("UPC", "upc"),
]

# Columns Spotify's deprecated audio-features endpoint would supply. Recorded so
# the reason they are absent is documented in one place rather than rediscovered.
UNAVAILABLE_COLUMNS = (
    "Popularity", "Danceability", "Energy", "Key", "Loudness", "Mode",
    "Speechiness", "Acousticness", "Instrumentalness", "Liveness", "Valence",
    "Tempo", "Time Signature", "Added By", "Added At",
)


def _track_uri(track: Any) -> str:
    """A stable locator for the track, in whatever service it came from."""
    sid = getattr(track, "spotify_id", None)
    if sid:
        return f"spotify:track:{sid}"
    url = getattr(track, "spotify_url", None)
    if url:
        return str(url)
    aid = getattr(track, "apple_music_id", None)
    if aid:
        return f"https://music.apple.com/us/song/{aid}"
    tid = getattr(track, "tidal_track_id", None)
    if tid:
        return f"https://tidal.com/track/{tid}"
    did = getattr(track, "deezer_track_id", None)
    if did:
        return f"https://www.deezer.com/track/{did}"
    asin = getattr(track, "amazon_asin", None)
    if asin:
        return f"https://music.amazon.com/albums/{asin}"
    return ""


def _explicit(track: Any) -> str:
    """'true'/'false', or blank when genuinely unknown.

    Measured: an Apple album returns None for 2 of 14 tracks - unknown, not
    clean - so a blank is the honest rendering and False must not stand in.
    """
    value = getattr(track, "is_explicit", None)
    if value is None:
        return ""
    return "true" if value else "false"


def _cell(track: Any, key: str) -> str:
    if key == "uri":
        return _track_uri(track)
    if key == "explicit":
        return _explicit(track)
    if key == "artists":
        return ", ".join(getattr(track, "artists", None) or [])
    if key == "album_artists":
        return ", ".join(getattr(track, "album_artists", None) or [])
    if key == "genres":
        return ", ".join(getattr(track, "genres", None) or [])
    value = getattr(track, key, None)
    if value is None:
        return ""
    return str(value)


def _relative_output_path(track: Any, prefs: dict, extension: str) -> str:
    """The path this track is EXPECTED to occupy under the library root.

    Mirrors LibraryOrganizer's album/playlist/single branches using the same
    pure helpers it uses, deliberately WITHOUT touching disk - an export must
    never create folders. Conflict suffixes (" (2)") are not applied, so this is
    the expected path rather than a guarantee.
    """
    prefs = prefs or {}
    playlist_name = getattr(track, "playlist_name", None)
    if playlist_name:
        folder = sanitize_filename_segment(str(playlist_name), prefs, max_len=120)
        number = getattr(track, "playlist_position", None) or getattr(track, "track_number", None)
        stem = build_track_stem(track, prefs, track_number=number, disc_number=1)
        return f"{folder}/{stem}{extension}"

    if (getattr(track, "request_kind", "") or "").lower() == "track":
        stem = build_single_track_stem(track, prefs)
        folder = build_folder_path(track, prefs)
        return f"{folder}/{stem}{extension}" if folder else f"{stem}{extension}"

    stem = build_track_stem(track, prefs)
    folder = build_folder_path(track, prefs)
    return f"{folder}/{stem}{extension}" if folder else f"{stem}{extension}"


def _used_columns(tracks: list) -> tuple[list[tuple[str, str]], list[str]]:
    """Split the Exportify columns into those with data and those without."""
    used, omitted = [], []
    for header, key in _CSV_COLUMNS:
        if any(_cell(t, key) for t in tracks):
            used.append((header, key))
        else:
            omitted.append(header)
    return used, omitted


def _write_csv(tracks, path, used):
    # utf-8-sig: Excel misreads plain UTF-8 accented titles. Antra-Web's importer
    # strips the BOM explicitly, so the round-trip is unaffected.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([h for h, _ in used])
        for track in tracks:
            writer.writerow([_cell(track, key) for _, key in used])


def _write_m3u8(tracks, path, title, prefs, extension):
    lines = ["#EXTM3U"]
    if title:
        lines.append(f"#PLAYLIST:{title}")
    for track in tracks:
        seconds = getattr(track, "duration_ms", None)
        seconds = int(seconds / 1000) if seconds else -1
        artists = ", ".join(getattr(track, "artists", None) or []) or "Unknown Artist"
        lines.append(f"#EXTINF:{seconds},{artists} - {getattr(track, 'title', '')}")
        lines.append(_relative_output_path(track, prefs, extension))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_xspf(tracks, path, title, source_url, prefs, extension):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<playlist version="1" xmlns="http://xspf.org/ns/0/">',
    ]
    if title:
        out.append(f"  <title>{_xml_escape(title)}</title>")
    if source_url:
        out.append(f"  <info>{_xml_escape(source_url)}</info>")
    out.append("  <creator>Antra</creator>")
    out.append("  <trackList>")
    for track in tracks:
        out.append("    <track>")
        out.append(f"      <location>{_xml_escape(_relative_output_path(track, prefs, extension))}</location>")
        out.append(f"      <title>{_xml_escape(str(getattr(track, 'title', '') or ''))}</title>")
        artists = ", ".join(getattr(track, "artists", None) or [])
        if artists:
            out.append(f"      <creator>{_xml_escape(artists)}</creator>")
        album = getattr(track, "album", None)
        if album:
            out.append(f"      <album>{_xml_escape(str(album))}</album>")
        number = getattr(track, "track_number", None)
        if number:
            out.append(f"      <trackNum>{int(number)}</trackNum>")
        duration = getattr(track, "duration_ms", None)
        if duration:
            out.append(f"      <duration>{int(duration)}</duration>")
        isrc = getattr(track, "isrc", None)
        if isrc:
            out.append(f"      <identifier>isrc:{_xml_escape(str(isrc))}</identifier>")
        art = getattr(track, "artwork_url", None)
        if art:
            out.append(f"      <image>{_xml_escape(str(art))}</image>")
        uri = _track_uri(track)
        if uri:
            out.append(f"      <info>{_xml_escape(uri)}</info>")
        out.append("    </track>")
    out.append("  </trackList>")
    out.append("</playlist>")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")


def _write_json(tracks, path, title, source_url):
    payload = {
        "generator": "Antra",
        "playlist": title or "",
        "source_url": source_url or "",
        "track_count": len(tracks),
        "tracks": [
            {
                "track_uri": _track_uri(t),
                "title": getattr(t, "title", "") or "",
                "artists": list(getattr(t, "artists", None) or []),
                "album": getattr(t, "album", "") or "",
                "album_artists": list(getattr(t, "album_artists", None) or []),
                "release_date": getattr(t, "release_date", None),
                "release_year": getattr(t, "release_year", None),
                "duration_ms": getattr(t, "duration_ms", None),
                "isrc": getattr(t, "isrc", None),
                "upc": getattr(t, "upc", None),
                "track_number": getattr(t, "track_number", None),
                "disc_number": getattr(t, "disc_number", None),
                "genres": list(getattr(t, "genres", None) or []),
                "label": getattr(t, "label", None),
                "composer": getattr(t, "composer", None),
                "explicit": getattr(t, "is_explicit", None),
                "artwork_url": getattr(t, "artwork_url", None),
                "source_service": getattr(t, "source_service", None),
            }
            for t in tracks
        ],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _write_txt(tracks, path, title, source_url):
    lines = []
    if title:
        lines.append(title)
        lines.append("=" * len(title))
    if source_url:
        lines.append(source_url)
    if lines:
        lines.append("")
    width = len(str(len(tracks)))
    for index, track in enumerate(tracks, 1):
        artists = ", ".join(getattr(track, "artists", None) or []) or "Unknown Artist"
        row = f"{str(index).rjust(width)}. {artists} - {getattr(track, 'title', '')}"
        album = getattr(track, "album", None)
        if album:
            row += f"  [{album}]"
        duration = getattr(track, "duration_ms", None)
        if duration:
            total = int(duration / 1000)
            row += f"  ({total // 60}:{total % 60:02d})"
        lines.append(row)
    lines.append("")
    lines.append(f"{len(tracks)} track{'s' if len(tracks) != 1 else ''}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def export_playlist(
    tracks: list,
    fmt: str,
    out_path: str,
    *,
    playlist_title: Optional[str] = None,
    source_url: Optional[str] = None,
    filename_preferences: Optional[dict] = None,
    audio_extension: str = ".flac",
) -> dict:
    """Write `tracks` to `out_path` in `fmt`. Returns a summary dict."""
    fmt = (fmt or "").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {fmt!r}. Expected one of {', '.join(EXPORT_FORMATS)}.")
    if not tracks:
        raise ValueError("Nothing to export - the tracklist is empty.")

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    prefs = filename_preferences or {}
    extension = audio_extension if audio_extension.startswith(".") else f".{audio_extension}"

    used, omitted = _used_columns(tracks)

    if fmt == "csv":
        _write_csv(tracks, out_path, used)
    elif fmt == "m3u8":
        _write_m3u8(tracks, out_path, playlist_title, prefs, extension)
    elif fmt == "xspf":
        _write_xspf(tracks, out_path, playlist_title, source_url, prefs, extension)
    elif fmt == "json":
        _write_json(tracks, out_path, playlist_title, source_url)
    else:
        _write_txt(tracks, out_path, playlist_title, source_url)

    return {
        "path": os.path.abspath(out_path),
        "format": fmt,
        "track_count": len(tracks),
        "columns": [h for h, _ in used] if fmt == "csv" else [],
        "omitted_columns": omitted if fmt == "csv" else [],
        "bytes": os.path.getsize(out_path),
    }


def suggested_filename(playlist_title: Optional[str], fmt: str, prefs: Optional[dict] = None) -> str:
    """A safe default filename for the save dialog."""
    stem = sanitize_filename_segment(str(playlist_title or "playlist"), prefs or {}, max_len=120)
    if not stem or stem == "Unknown":
        stem = "playlist"
    return f"{stem}{EXPORT_EXTENSIONS.get(fmt, '.txt')}"
