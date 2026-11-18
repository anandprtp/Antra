"""v2.0.0 DMX-2 — negative control for test_text_transforms.py.

A passing suite proves nothing until it has been shown to fail against broken
code. Each sabotage below reverts one piece of DMX-2 in place, runs the real
suite as a subprocess, and asserts it goes RED — then restores the file and
verifies it is byte-identical to what was there before.

The suite is re-run clean first and last, so a sabotage that fails to restore
is caught here rather than being left on disk.

Run:  python -m antra.utils.test_text_transforms_negctl
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PREFS = ROOT / "antra_shared" / "filename_prefs.py"
TAGGER = ROOT / "antra" / "utils" / "tagger.py"
ORG = ROOT / "antra" / "utils" / "organizer.py"

SUITE = ["-m", "antra.utils.test_text_transforms"]


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable] + SUITE, cwd=str(ROOT),
        capture_output=True, text=True, errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode == 0 and ", 0 failed" in out
    return passed, out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# (label, file, old, new) — `old` must appear exactly once.
SABOTAGES: list[tuple[str, Path, str, str]] = [
    (
        "filenames stop using the transformed title",
        PREFS,
        '        "title": str(tx.title or ""),',
        '        "title": str(getattr(track, "title", "") or ""),',
    ),
    (
        "FLAC tag stops using the transformed title",
        TAGGER,
        '        if tx.title and self._want("title"):\r\n            audio["title"] = tx.title',
        '        if tx.title and self._want("title"):\r\n            audio["title"] = track.title',
    ),
    (
        "MP3 writes the multi-value list head instead of the joined string",
        TAGGER,
        "            audio.add(TPE1(encoding=3, text=tx.artist_joined))",
        '            audio.add(TPE1(encoding=3, text=(tx.artist_values or [""])[0]))',
    ),
    (
        "transform_text MUTATES the track (breaks matching)",
        PREFS,
        "    return TransformedText(\n        title=title,",
        "    try:\n        track.title = title\n    except Exception:\n        pass\n"
        "    return TransformedText(\n        title=title,",
    ),
    (
        "sentence case walks forward to the first letter anywhere",
        PREFS,
        "        lowered = value.lower()\n        if lowered[:1].isalpha():\n"
        "            return lowered[:1].upper() + lowered[1:]\n        return lowered",
        "        lowered = value.lower()\n        for _i, _c in enumerate(lowered):\n"
        "            if _c.isalpha():\n"
        "                return lowered[:_i] + _c.upper() + lowered[_i + 1:]\n"
        "        return lowered",
    ),
    (
        "album-version strip also eats (Live) and (Remix)",
        PREFS,
        "          [\\(\\[]\\s*album\\s+(?:version|ver\\.?)\\s*[\\)\\]]   # (Album Version)",
        "          [\\(\\[]\\s*(?:album\\s+(?:version|ver\\.?)|live|remix)\\s*[\\)\\]]",
    ),
    (
        "dedupe drops an artist whose name is a substring of another",
        PREFS,
        "        if key in seen:\n            continue\n        seen.add(key)\n        out.append(text)",
        "        if key in seen or any(key in s or s in key for s in seen):\n"
        "            continue\n        seen.add(key)\n        out.append(text)",
    ),
    (
        "identity keys stop covering the transformed title",
        ORG,
        "        transformed = (tx.title or \"\", list(tx.artists or []))\r\n"
        "        return [raw] if transformed == raw else [raw, transformed]",
        "        _ = tx\r\n        return [raw]",
    ),
    (
        "Various Artists swap removed from the filename path",
        PREFS,
        "    album_artist_token = resolve_various_artists(\n"
        '        album_artist or artist or "", tx.artists, prefs\n    )',
        '    album_artist_token = album_artist or artist or ""',
    ),
    (
        'whitespace "dash" goes back to being a dead option',
        PREFS,
        '    if policy in ("hyphen", "dash"):',
        '    if policy == "hyphen":',
    ),
    (
        "remove_duplicate_artists defaults to OFF",
        PREFS,
        '    "remove_duplicate_artists": True,\n    "featured_artists_mode": "nothing",',
        '    "remove_duplicate_artists": False,\n    "featured_artists_mode": "nothing",',
    ),
]


def main() -> int:
    print("control run (must PASS)")
    ok, out = run_suite()
    if not ok:
        print(out[-3000:])
        print("ABORT: the suite does not pass before any sabotage")
        return 1
    print("  OK\n")

    caught = 0
    missed: list[str] = []
    for label, path, old, new in SABOTAGES:
        original = path.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            print("  SKIP-ERROR  %-58s anchor matched %d" % (label, text.count(old)))
            missed.append(label + " (bad anchor)")
            continue
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            failed, _out = run_suite()
            if failed:
                print("  MISSED   %s" % label)
                missed.append(label)
            else:
                caught += 1
                print("  caught   %s" % label)
        finally:
            path.write_bytes(original)
            assert digest(path) == before, "%s: restore failed for %s" % (label, path.name)

    print("\nfinal run (must PASS again)")
    ok, out = run_suite()
    if not ok:
        print(out[-3000:])
        print("ABORT: a sabotage was not restored cleanly")
        return 1
    print("  OK")

    print("\n%d/%d sabotages detected" % (caught, len(SABOTAGES)))
    for m in missed:
        print("  MISSED: %s" % m)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
