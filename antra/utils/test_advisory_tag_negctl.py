"""Negative control for ADV-1 (issue #43).

Sabotages the real `tagger.py` / `engine.py` in place, one edit at a time,
and asserts `test_advisory_tag` FAILS each time. A suite that cannot fail
proves nothing, and several of these are the shapes this feature would most
plausibly regress into.

Every file is restored byte-identically (md5 checked) before exiting, and on
any exception.

Run:  python -m antra.utils.test_advisory_tag_negctl
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAGGER = os.path.join(ROOT, "antra", "utils", "tagger.py")
ENGINE = os.path.join(ROOT, "antra", "core", "engine.py")


def read(path: str) -> str:
    return io.open(path, encoding="utf-8", newline="").read()


def write(path: str, text: str) -> None:
    io.open(path, "w", encoding="utf-8", newline="").write(text)


def md5(path: str) -> str:
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# (label, file, old, new). `old` must appear exactly once.
SABOTAGES = [
    (
        "the write is removed entirely (the pre-feature state)",
        TAGGER,
        '        if self._want("advisory"):',
        '        if False and self._want("advisory"):',
    ),
    (
        "unknown explicitness is treated as clean",
        TAGGER,
        "        if flag is None:\n            return None",
        "        if flag is None:\n            return ADVISORY_CLEAN",
    ),
    (
        "1 and 2 are swapped",
        TAGGER,
        "        return ADVISORY_EXPLICIT if flag else ADVISORY_CLEAN",
        "        return ADVISORY_CLEAN if flag else ADVISORY_EXPLICIT",
    ),
    (
        "the delivered file's flag is ignored",
        TAGGER,
        "        flag = delivered_explicit if delivered_explicit is not None else track.is_explicit",
        "        flag = track.is_explicit",
    ),
    (
        "the delivered flag overrides even when the adapter said nothing",
        TAGGER,
        "        flag = delivered_explicit if delivered_explicit is not None else track.is_explicit",
        "        flag = delivered_explicit",
    ),
    (
        "the toggle is not honoured",
        TAGGER,
        '        if self._want("advisory"):',
        "        if True:",
    ),
    (
        "advisory is dropped from the toggle registry",
        TAGGER,
        '    "spotify_id", "audio_traits", "advisory",',
        '    "spotify_id", "audio_traits",',
    ),
    (
        "FLAC gets the atom too (scope creep past ALAC/AAC)",
        TAGGER,
        "    def _tag_flac(self, path: str, track: TrackMetadata):\n        audio = FLAC(path)",
        "    def _tag_flac(self, path: str, track: TrackMetadata):\n        audio = FLAC(path)\n"
        '        audio["rtng"] = ["1" if track.is_explicit else "2"]',
    ),
    (
        "the engine stops handing over the delivered flag",
        ENGINE,
        '                    delivered_explicit=getattr(result, "is_explicit", None),',
        "",
    ),
    (
        "the engine wires the keyword to the wrong field",
        ENGINE,
        '                    delivered_explicit=getattr(result, "is_explicit", None),',
        '                    delivered_explicit=getattr(result, "is_lossless", None),',
    ),
]


def run_suite() -> bool:
    """True when the suite PASSES."""
    r = subprocess.run(
        [sys.executable, "-m", "antra.utils.test_advisory_tag"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return r.returncode == 0


def main() -> int:
    originals = {p: read(p) for p in (TAGGER, ENGINE)}
    hashes = {p: md5(p) for p in originals}

    caught = 0
    try:
        if not run_suite():
            print("BASELINE FAILS — fix the suite before trusting this control")
            return 2
        print("baseline: suite passes\n")

        for label, path, old, new in SABOTAGES:
            src = originals[path]
            # Anchors are written with "\n"; these files are CRLF. Deriving the
            # ending per file rather than assuming one is not pedantry — the
            # same mistake made an earlier control report every anchor as
            # "found 0x" and read as 7/8 passing.
            nl = "\r\n" if "\r\n" in src else "\n"
            if nl != "\n":
                old, new = old.replace("\n", nl), new.replace("\n", nl)
            n = src.count(old)
            if n != 1:
                print("  REFUSING  %s — anchor found %dx" % (label, n))
                continue
            try:
                write(path, src.replace(old, new, 1))
                failed = not run_suite()
            finally:
                write(path, src)
            caught += failed
            print("  %s  %s" % ("CAUGHT " if failed else "MISSED ", label))
    finally:
        for p, src in originals.items():
            write(p, src)
        for p, h in hashes.items():
            state = "identical" if md5(p) == h else "*** DIFFERS ***"
            print("restored %s: %s" % (os.path.basename(p), state))

    print("\n%d/%d caught" % (caught, len(SABOTAGES)))
    return 0 if caught == len(SABOTAGES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
