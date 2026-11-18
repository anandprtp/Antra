"""Sidecar files — .cue / .nfo / .json  (v2.0.0 PORT-2)

WHY THERE IS NO .m3u TOGGLE HERE
--------------------------------
Antra-Web ships four sidecar toggles, m3u included, because on the web the
organizer's playlist manifest was generated into a temp dir and then thrown
away by the packaging step — nobody ever received it, so a new m3u superseded
a file that did not exist in practice.

On the desktop that file is REAL: `LibraryOrganizer.write_playlist_manifest()`
writes `<Playlist Name>.m3u` into the user's library on every playlist download
and it is still there afterwards. Adding an m3u toggle would either duplicate
that file or move it, and both are worse than leaving a shipped, working feature
alone. So this module ships the three formats the desktop genuinely lacks, which
is also exactly what PORT-2's own title asks for.

The one real gap that leaves is that an ALBUM download gets no m3u (the manifest
is playlist-only). A `.cue` covers that role far better for an album, which is
what it is for. If an album m3u is ever wanted it is a fourth toggle and a
~12-line builder — deliberately not built on a guess.

THE ORDERING RULE
-----------------
Sidecars are built LAST, from the paths that were ACTUALLY WRITTEN — never from
the requested tracklist. A track that failed has no file on disk, and a cue
sheet referencing it would send the player to a dead path.

SKIPPED COUNTS, AND THAT IS A DESKTOP-SPECIFIC DECISION
-------------------------------------------------------
Web's rule is "completed only", because in its temp dir a skipped track has no
file. The desktop writes straight into the library, so `DownloadStatus.SKIPPED`
carries a `file_path` pointing at a real, already-owned file that is part of the
same release. Excluding those would make a re-run of a mostly-complete album
emit a cue describing the one track that happened to be missing. So the filter
is "has a file_path that exists on disk", not "was downloaded in this run".

THE COVERAGE RULE (desktop-only, and the reason this cannot just be ported)
--------------------------------------------------------------------------
Web always writes into a fresh temp dir, so a sidecar can never overwrite one
that describes more than the current run knows about. The desktop writes into a
persistent library, where three separate paths reach this code with a PARTIAL
view of a folder:

  * a single-track URL dropped into a folder that already holds the album,
  * a retry of one failed track, and
  * a cancelled run.

Any of those would silently shrink a correct 12-track .nfo to a 1-track one.

So a directory's sidecars are written only when this run accounts for EVERY
audio file currently in that directory. That makes the sidecar's implicit claim
— "this describes this folder" — true by construction rather than by hope, and
it resolves all three cases without a special case for any of them:

  1 track into a fresh folder        1 == 1   write   (a genuine single)
  1 track into an existing album     1 <  12  skip    (keeps the good .nfo)
  full re-run, 11 already owned      12 == 12 write   (SKIPPED counts)
  album where 2 tracks failed        10 == 10 write   (describes what is there)
  ...but a past run got those 2      10 <  12 skip    (never shrink)

EXPLICIT PATHS, NEVER AN EXTENSION WALK
---------------------------------------
The writer writes exactly the paths planned here. It does NOT walk the folder
for `*.json`, because `LibraryOrganizer` keeps `.antra_state.json` in the
library root — a blanket walk would ship the user our internal dedup state.

GROUPING
--------
Sidecars land in the directory holding the tracks they describe, grouped by that
directory, and named after it — the organizer already built that name from the
user's own templates, so a sidecar cannot disagree with the folder it sits in.

  - one .cue per (directory, disc)   — cue FILE lines are same-directory only
  - one .nfo per directory
  - one .json per directory
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from antra.utils.fsutil import write_atomic
from antra.utils.longpath import extended_path
# Single source of truth — a local copy of this tuple would drift the moment a
# new output container is added.
from antra.utils.organizer import SUPPORTED_AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)

__all__ = [
    "SidecarFile",
    "group_entries",
    "filter_to_covered",
    "plan_sidecars",
    "write_sidecars",
    "write_release_sidecars",
    "build_cue",
    "build_nfo",
    "build_json",
]

# CUE sheets number tracks 01-99. There is no representation beyond that, so a
# directory holding more is truncated rather than emitting invalid TRACK lines.
_CUE_MAX_TRACKS = 99

_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")

# ffmpeg / foobar2000 accept these as cue FILE types; anything else falls back
# to WAVE, which every parser tolerates.
_CUE_FILE_TYPE = {
    ".flac": "WAVE",
    ".wav": "WAVE",
    ".m4a": "AIFF",
    ".mp4": "AIFF",
    ".mp3": "MP3",
    ".aac": "AIFF",
    ".ogg": "WAVE",
    ".opus": "WAVE",
}

# .cue and .nfo are CRLF by convention (NFO is a scene format and has always
# been CRLF; old CD-burning software is stricter about cue line endings than
# ffmpeg or foobar2000 are). .json follows the JSON convention and stays LF.
_CRLF_KINDS = {"cue", "nfo"}


class SidecarFile:
    """One planned file: an absolute path plus the exact text to write."""

    __slots__ = ("path", "text", "kind")

    def __init__(self, path: str, text: str, kind: str):
        self.path = path
        self.text = text
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SidecarFile(%r, kind=%r, %d chars)" % (self.path, self.kind, len(self.text))


# -- small helpers ----------------------------------------------------------

def _safe(name: str, max_len: int = 120) -> str:
    """Filesystem-safe base name. Mirrors the organizer's conservative rules.

    Leading dots are stripped as well as trailing ones. Traversal is already
    impossible — every separator is removed first, so "../../etc" cannot
    address anything outside the directory — but it would still collapse to
    "......etc", a hidden file on Unix and an awkward one everywhere else.
    """
    name = (name or "").replace(":", " -")
    name = "".join(c for c in name if c not in '\\/*?"<>|' and ord(c) >= 32)
    name = name.strip().strip(". ")
    return (name[:max_len].strip() or "Untitled")


def _cue_quote(value: Optional[str]) -> str:
    """CUE has no escape sequence for a double quote — replace it."""
    v = (value or "").replace('"', "'").replace("\r", " ").replace("\n", " ")
    # Long values break some parsers; the spec's practical ceiling is 80 chars.
    return '"' + v.strip()[:80] + '"'


def _artists(track: Any) -> str:
    vals = list(getattr(track, "artists", None) or [])
    return ", ".join(v for v in vals if v) or "Unknown Artist"


def _album_artists(track: Any) -> str:
    vals = list(getattr(track, "album_artists", None) or [])
    return ", ".join(v for v in vals if v) or _artists(track)


def _seconds(track: Any) -> Optional[int]:
    ms = getattr(track, "duration_ms", None)
    if not ms:
        return None
    try:
        return int(round(int(ms) / 1000))
    except (TypeError, ValueError):
        return None


def _clean_isrc(track: Any) -> Optional[str]:
    isrc = (getattr(track, "isrc", None) or "").replace("-", "").upper().strip()
    return isrc if _ISRC_RE.match(isrc) else None


def _disc_of(track: Any) -> int:
    try:
        return int(getattr(track, "disc_number", None) or 1)
    except (TypeError, ValueError):
        return 1


# -- grouping (pure) --------------------------------------------------------

def group_entries(entries: Iterable[tuple]) -> "dict[str, list]":
    """Bucket (track, absolute_path) pairs by their parent directory.

    Download order is preserved inside each bucket — that is the order the cue
    and nfo listings use, and it is the only order we can honestly claim (the
    engine downloads in parallel but reports results in track order).
    """
    groups: "dict[str, list]" = {}
    seen: set = set()
    for entry in entries:
        if not entry or not entry[1]:
            continue
        track, raw = entry
        p = Path(os.path.normpath(os.path.abspath(str(raw))))
        # One file is one entry: a track listed twice (the second a SKIPPED that
        # points at the first's file) must not count twice toward the coverage
        # rule or appear twice in a cue / nfo.
        key = os.path.normcase(str(p))
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(str(p.parent), []).append((track, p))
    return groups


def _group_name(directory: str, tracks: list) -> str:
    """Base name for the sidecars in a directory.

    The directory's own name is used, because the organizer already built it
    from the release title with the user's own templates — so the sidecar name
    cannot disagree with the folder it sits in. Metadata is only a fallback for
    the pathological case of a file landing at a drive root.
    """
    name = Path(directory).name
    if name:
        return _safe(name)
    first = tracks[0][0] if tracks else None
    if first is not None:
        return _safe(getattr(first, "playlist_name", None)
                     or getattr(first, "album", None)
                     or getattr(first, "title", None)
                     or "Antra")
    return "Antra"


# -- coverage filter (touches the filesystem) -------------------------------

def _audio_files_in(directory: str) -> Optional[int]:
    """Count audio files in a directory, or None when it cannot be read."""
    try:
        count = 0
        with os.scandir(extended_path(directory)) as it:
            for e in it:
                try:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in SUPPORTED_AUDIO_EXTENSIONS:
                        count += 1
                except OSError:
                    continue
        return count
    except OSError:
        return None


def filter_to_covered(groups: "dict[str, list]") -> "dict[str, list]":
    """Keep only directories this run fully accounts for. See COVERAGE RULE.

    Entries whose file is no longer on disk are dropped first, so the comparison
    is between two things that are both true right now.
    """
    kept: "dict[str, list]" = {}
    for directory, tracks in groups.items():
        present = [(t, p) for t, p in tracks if os.path.exists(extended_path(str(p)))]
        if not present:
            continue
        on_disk = _audio_files_in(directory)
        if on_disk is None:
            # Cannot read the directory — refuse rather than guess.
            logger.debug("[Sidecars] cannot read %s — skipping", directory)
            continue
        if len(present) < on_disk:
            logger.info(
                "[Sidecars] %s holds %d audio file(s) but this run only accounts for %d "
                "— leaving its sidecars alone rather than describing part of the folder.",
                Path(directory).name or directory, on_disk, len(present),
            )
            continue
        kept[directory] = present
    return kept


# -- builders (pure) --------------------------------------------------------

def build_cue(tracks: list, disc: Optional[int], total_discs: Optional[int]) -> str:
    """One CUE sheet for one disc, in the file-per-track layout.

    Every track gets its own FILE line, which is what foobar2000 / Plex expect
    when the release is not a single ripped image. INDEX is always 00:00:00 for
    the same reason.
    """
    first = tracks[0][0]
    out: list = []
    out.append("REM COMMENT " + _cue_quote("Generated by Antra"))
    year = getattr(first, "release_year", None)
    if year:
        try:
            out.append("REM DATE %d" % int(year))
        except (TypeError, ValueError):
            pass
    genres = list(getattr(first, "genres", None) or [])
    if genres:
        out.append("REM GENRE " + _cue_quote(genres[0]))
    if disc and (total_discs or 0) > 1:
        out.append("REM DISCNUMBER %d" % int(disc))
        out.append("REM TOTALDISCS %d" % int(total_discs))
    out.append("PERFORMER " + _cue_quote(_album_artists(first)))
    out.append("TITLE " + _cue_quote(getattr(first, "album", None)
                                     or getattr(first, "playlist_name", None)
                                     or "Unknown Album"))

    shown = tracks[:_CUE_MAX_TRACKS]

    def _real_number(track):
        try:
            return int(getattr(track, "track_number", None))
        except (TypeError, ValueError):
            return None

    # The real numbers are used only when they form a valid sheet: each 1-99 and
    # strictly ascending (gaps are fine - a partial album). A playlist folder
    # holds tracks from different albums, all "track 1", which would emit
    # `TRACK 01` repeatedly; then the position in the sheet is used instead.
    reals = [_real_number(t) for t, _ in shown]
    use_real = (
        all(n is not None and 1 <= n <= _CUE_MAX_TRACKS for n in reals)
        and all(a < b for a, b in zip(reals, reals[1:]))
    )
    for position, (track, path) in enumerate(shown, start=1):
        num = reals[position - 1] if use_real else position
        ftype = _CUE_FILE_TYPE.get(path.suffix.lower(), "WAVE")
        out.append("FILE %s %s" % (_cue_quote(path.name), ftype))
        out.append("  TRACK %02d AUDIO" % num)
        out.append("    TITLE " + _cue_quote(getattr(track, "title", None) or path.stem))
        out.append("    PERFORMER " + _cue_quote(_artists(track)))
        isrc = _clean_isrc(track)
        if isrc:
            out.append("    ISRC " + isrc)
        out.append("    INDEX 01 00:00:00")

    if len(tracks) > _CUE_MAX_TRACKS:
        out.append("REM TRUNCATED %d more tracks - a CUE sheet cannot address "
                   "beyond track 99" % (len(tracks) - _CUE_MAX_TRACKS))
    return "\n".join(out) + "\n"


def _fmt_duration(secs: Optional[int]) -> str:
    if secs is None:
        return "--:--"
    return "%d:%02d" % (secs // 60, secs % 60)


def build_nfo(tracks: list, download_format: str = "",
              generated_at: Optional[datetime] = None) -> str:
    """Human-readable metadata dump."""
    when = generated_at or datetime.now(timezone.utc)
    first = tracks[0][0]
    title = getattr(first, "playlist_name", None) or getattr(first, "album", None) or "Unknown"
    total_secs = sum(s for s in (_seconds(t) for t, _ in tracks) if s)

    header = [
        title,
        "=" * max(3, min(len(title), 78)),
        "",
        "Artist       : " + _album_artists(first),
    ]
    year = getattr(first, "release_year", None)
    if year:
        header.append("Year         : %s" % year)
    label = getattr(first, "label", None)
    if label:
        header.append("Label        : %s" % label)
    upc = getattr(first, "upc", None)
    if upc:
        header.append("UPC          : %s" % upc)
    genres = list(getattr(first, "genres", None) or [])
    if genres:
        header.append("Genre        : %s" % ", ".join(genres))
    header.append("Tracks       : %d" % len(tracks))
    if total_secs:
        if total_secs >= 3600:
            header.append("Total time   : %dh %02dm" % (total_secs // 3600, (total_secs % 3600) // 60))
        else:
            header.append("Total time   : %dm %02ds" % (total_secs // 60, total_secs % 60))
    if download_format:
        header.append("Requested    : %s" % download_format)
    header.append("Downloaded   : %s" % when.strftime("%Y-%m-%d %H:%M UTC"))
    header.append("Generated by : Antra")
    header.append("")
    header.append("-" * 78)
    header.append("")

    # A multi-disc release listed flat would restart at "1." on disc 2, which
    # reads as a mistake. Qualify the number with the disc in that case only.
    multi_disc = len({_disc_of(t) for t, _ in tracks}) > 1

    body: list = []
    for track, path in tracks:
        num = getattr(track, "track_number", None) or 0
        label_txt = ("%d-%d." % (_disc_of(track), num)) if multi_disc else ("%d." % num)
        body.append("%5s %s" % (label_txt, getattr(track, "title", None) or path.stem))
        body.append("     Artist   : " + _artists(track))
        body.append("     Duration : " + _fmt_duration(_seconds(track)))
        isrc = _clean_isrc(track)
        if isrc:
            body.append("     ISRC     : " + isrc)
        body.append("     File     : " + path.name)
        body.append("")

    return "\n".join(header + body)


def build_json(tracks: list, download_format: str = "",
               generated_at: Optional[datetime] = None) -> str:
    """Machine-readable metadata dump."""
    when = generated_at or datetime.now(timezone.utc)
    first = tracks[0][0]
    payload = {
        "generator": "Antra",
        "generated_at": when.isoformat().replace("+00:00", "Z"),
        "requested_format": download_format or None,
        "release": {
            "title": getattr(first, "playlist_name", None) or getattr(first, "album", None),
            "kind": getattr(first, "request_kind", None),
            "album_artists": (list(getattr(first, "album_artists", None) or [])
                              or list(getattr(first, "artists", None) or [])),
            "year": getattr(first, "release_year", None),
            "release_date": getattr(first, "release_date", None),
            "label": getattr(first, "label", None),
            "upc": getattr(first, "upc", None),
            "total_tracks": getattr(first, "total_tracks", None),
            "total_discs": getattr(first, "total_discs", None),
            "genres": list(getattr(first, "genres", None) or []),
            "track_count": len(tracks),
        },
        "tracks": [
            {
                "file": path.name,
                "title": getattr(t, "title", None),
                "artists": list(getattr(t, "artists", None) or []),
                "album": getattr(t, "album", None),
                "track_number": getattr(t, "track_number", None),
                "disc_number": getattr(t, "disc_number", None),
                "playlist_position": getattr(t, "playlist_position", None),
                "duration_ms": getattr(t, "duration_ms", None),
                "isrc": _clean_isrc(t),
                "composer": getattr(t, "composer", None),
                "explicit": getattr(t, "is_explicit", None),
                "release_date": getattr(t, "release_date", None),
            }
            for t, path in tracks
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# -- the planner (pure) -----------------------------------------------------

def plan_sidecars(
    groups: "dict[str, list]",
    want_cue: bool = False,
    want_nfo: bool = False,
    want_json: bool = False,
    download_format: str = "",
    generated_at: Optional[datetime] = None,
) -> list:
    """Plan every sidecar for already-grouped, already-covered directories.

    Returns absolute paths plus their content. Writing is a separate step so the
    whole of the interesting logic can be asserted on the bytes it produces.
    """
    if not (want_cue or want_nfo or want_json):
        return []

    when = generated_at or datetime.now(timezone.utc)
    out: list = []

    for directory in sorted(groups):
        tracks = groups[directory]
        if not tracks:
            continue
        base = _group_name(directory, tracks)

        if want_cue:
            # One sheet per disc: a cue's FILE entries and its disc metadata
            # only make sense for a single disc.
            discs: "dict[int, list]" = {}
            for track, path in tracks:
                discs.setdefault(_disc_of(track), []).append((track, path))
            multi = len(discs) > 1
            for disc in sorted(discs):
                name = ("%s (Disc %d).cue" % (base, disc)) if multi else (base + ".cue")
                total_discs = (getattr(discs[disc][0][0], "total_discs", None)
                               or (len(discs) if multi else 1))
                out.append(SidecarFile(os.path.join(directory, name),
                                       build_cue(discs[disc], disc, total_discs), "cue"))

        if want_nfo:
            out.append(SidecarFile(os.path.join(directory, base + ".nfo"),
                                   build_nfo(tracks, download_format, when), "nfo"))
        if want_json:
            out.append(SidecarFile(os.path.join(directory, base + ".json"),
                                   build_json(tracks, download_format, when), "json"))

    return out


# -- the writer (touches the filesystem) ------------------------------------

def write_sidecars(plans: Iterable[SidecarFile]) -> "list[str]":
    """Write each planned file. One bad path never stops the rest."""
    written: "list[str]" = []
    for plan in plans:
        try:
            newline = "\r\n" if plan.kind in _CRLF_KINDS else "\n"
            write_atomic(plan.path, plan.text, encoding="utf-8", newline=newline)
            written.append(plan.path)
        except (OSError, ValueError) as e:  # ValueError covers UnicodeEncodeError
            logger.warning("[Sidecars] could not write %s: %s", plan.path, e)
    return written


def write_release_sidecars(
    entries: Iterable[tuple],
    want_cue: bool = False,
    want_nfo: bool = False,
    want_json: bool = False,
    download_format: str = "",
    generated_at: Optional[datetime] = None,
) -> "list[str]":
    """group -> coverage filter -> plan -> write. The engine's single entry point."""
    if not (want_cue or want_nfo or want_json):
        return []
    groups = filter_to_covered(group_entries(entries))
    if not groups:
        return []
    plans = plan_sidecars(groups, want_cue, want_nfo, want_json,
                          download_format, generated_at)
    written = write_sidecars(plans)
    if written:
        logger.info("[Sidecars] wrote %d file(s) across %d folder(s).",
                    len(written), len(groups))
    return written
