"""Negative control for the DMX-1 suite.

A passing suite proves nothing until it has failed against the code being
replaced. Each sabotage below reverts one specific part of DMX-1 in
`tagger.py`, runs the real suite as a subprocess, and asserts it goes RED.
The file is restored byte-for-byte after every case, and a clean run is
asserted before and after so the control cannot leave the tree damaged.

Run:  python -m antra.utils.test_tag_toggles_negctl
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAGGER = ROOT / "antra" / "utils" / "tagger.py"
SUITE = ["python", "-m", "antra.utils.test_tag_toggles"]

CR_LF = "\r\n"
LF = "\n"


def run_suite() -> bool:
    """True when the suite passes."""
    proc = subprocess.run(SUITE, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0


# (label, old, new) — each `old` must appear exactly once in the LF-normalised
# source. Anchors use plain newlines; tagger.py is pure CRLF on disk.
SABOTAGES = [
    (
        "_want() always True — every toggle becomes a no-op",
        "return field not in self.disabled_tags",
        "return True",
    ),
    (
        "disabled ids not lowercased — ' TITLE ' stops matching 'title'",
        "str(name).strip().lower() for name in (disabled_tags or ()) if str(name).strip()",
        "str(name) for name in (disabled_tags or ()) if str(name).strip()",
    ),
    (
        "track_total no longer separable from track_number (FLAC)",
        '            if track.total_tracks and self._want("track_total"):\n'
        '                tn += f"/{track.total_tracks}"\n'
        '            audio["tracknumber"] = tn',
        "            if track.total_tracks:\n"
        '                tn += f"/{track.total_tracks}"\n'
        '            audio["tracknumber"] = tn',
    ),
    (
        "cover gate removed (FLAC) — artwork written even when switched off",
        '        if self._want("cover"):\n'
        "            artwork = self._fetch_artwork(track.artwork_url)\n"
        "            if artwork:\n"
        "                artwork_data, mime, width, height, depth = artwork",
        "        if True:\n"
        "            artwork = self._fetch_artwork(track.artwork_url)\n"
        "            if artwork:\n"
        "                artwork_data, mime, width, height, depth = artwork",
    ),
    (
        "YEAR derived from the FORMATTED date — breaks under dmy/mdy",
        "year_str = self._year_tag(track)",
        'year_str = (date_str or "")[:4] if (date_str or "")[:4].isdigit() else None',
    ),
    (
        "ID3 TDRC given the free-text date — silently writes 0012-04-2024",
        "date_str = self._date_tag(track, iso_only=True)",
        "date_str = self._date_tag(track)",
    ),
    (
        "write_id3v1 ignored — the legacy tag is always written",
        "return 2 if self.write_id3v1 else 0",
        "return 2",
    ),
    (
        "lyrics write-gate removed (MP4) — the LRC backfills a disabled tag",
        'if best_plain and self._want("lyrics"):',
        "if best_plain:",
    ),
    (
        "render_date no longer falls back to ISO for an unknown format",
        'if not match or fmt not in ("dmy", "mdy"):',
        "if not match:",
    ),
    (
        "lyric sidecars ignore the toggles",
        'if track.synced_lyrics and self._want("synced_lyrics"):',
        "if track.synced_lyrics:",
    ),
]


def main() -> int:
    original = TAGGER.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    as_lf = original.decode("utf-8").replace(CR_LF, LF)

    print("control: suite must pass before any sabotage")
    if not run_suite():
        print("  ABORT — the suite is already failing; fix that first")
        return 1
    print("  clean run OK")

    caught = 0
    missed = []
    try:
        for label, old, new in SABOTAGES:
            count = as_lf.count(old)
            if count != 1:
                missed.append("%s (anchor matched %d x — sabotage invalid)" % (label, count))
                print("  INVALID  %s — anchor matched %d x" % (label, count))
                continue
            broken = as_lf.replace(old, new, 1).replace(LF, CR_LF)
            TAGGER.write_bytes(broken.encode("utf-8"))
            if run_suite():
                missed.append(label)
                print("  MISSED   %s" % label)
            else:
                caught += 1
                print("  caught   %s" % label)
            TAGGER.write_bytes(original)
    finally:
        TAGGER.write_bytes(original)

    restored = hashlib.sha256(TAGGER.read_bytes()).hexdigest()
    print("\nfile restored byte-identically:", restored == digest)
    print("control: suite must pass again")
    ok_after = run_suite()
    print("  clean run OK:", ok_after)

    print("\n%d/%d sabotages detected" % (caught, len(SABOTAGES)))
    for m in missed:
        print("  -", m)
    return 0 if (caught == len(SABOTAGES) and restored == digest and ok_after) else 1


if __name__ == "__main__":
    raise SystemExit(main())
