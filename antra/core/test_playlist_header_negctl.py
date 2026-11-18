"""Negative control for the playlist-header fixes.

Sabotages `spotify.py` / `service.py` in place, one edit at a time, and asserts
`test_playlist_header` FAILS each time. Sabotage 1 and sabotage 4 are the
reported bugs put back verbatim.

Every file is restored byte-identically (md5 checked), including on exceptions.

Run:  python -m antra.core.test_playlist_header_negctl
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPOTIFY = os.path.join(ROOT, "antra", "core", "spotify.py")
SERVICE = os.path.join(ROOT, "antra", "core", "service.py")


def read(p):
    return io.open(p, encoding="utf-8", newline="").read()


def write(p, t):
    io.open(p, "w", encoding="utf-8", newline="").write(t)


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


SABOTAGES = [
    (
        "the owner is never read (the reported bug)",
        SPOTIFY,
        "                    if not playlist_owner:\n"
        "                        _owner = (playlist_data.get(\"ownerV2\") or {}).get(\"data\") or {}",
        "                    if False:\n"
        "                        _owner = (playlist_data.get(\"ownerV2\") or {}).get(\"data\") or {}",
    ),
    (
        "the owner is read from the wrong field",
        SPOTIFY,
        'playlist_owner = _owner.get("name") or _owner.get("username") or None',
        'playlist_owner = _owner.get("display_name") or None',
    ),
    (
        "the cover is picked by position again (60px)",
        SPOTIFY,
        "                                playlist_artwork = max(\n"
        "                                    _srcs,\n"
        "                                    key=lambda s: (s or {}).get(\"width\") or 0,\n"
        "                                ).get(\"url\")",
        '                                playlist_artwork = _srcs[-1].get("url")',
    ),
    (
        "the year fill goes back to a serial loop (the 37s)",
        SERVICE,
        "            with ThreadPoolExecutor(max_workers=workers) as pool:\n"
        "                list(pool.map(_fill, missing))",
        "            for _t in missing:\n"
        "                _fill(_t)",
    ),
    (
        "the year fill runs unbounded (no pool ceiling)",
        SERVICE,
        "        workers = min(_YEAR_FILL_WORKERS, len(missing))",
        "        workers = max(1, len(missing))",
    ),
    (
        "the year fill silently skips tracks",
        SERVICE,
        "        missing = [t for t in tracks if not t.release_year]",
        "        missing = [t for t in tracks if not t.release_year][:1]",
    ),
    (
        "a failing fill aborts the whole batch",
        SERVICE,
        "            try:\n"
        "                spotify.enrich_public_track_metadata(track)\n"
        "            except Exception as e:\n"
        "                logger.debug(\"[Service] Year fill failed for '%s': %s\", track.title, e)",
        "            spotify.enrich_public_track_metadata(track)",
    ),
]


def run_suite() -> bool:
    r = subprocess.run([sys.executable, "-m", "antra.core.test_playlist_header"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    originals = {p: read(p) for p in (SPOTIFY, SERVICE)}
    hashes = {p: md5(p) for p in originals}
    caught = 0
    try:
        if not run_suite():
            print("BASELINE FAILS -- fix the suite before trusting this control")
            return 2
        print("baseline: suite passes\n")
        for label, path, old, new in SABOTAGES:
            src = originals[path]
            # anchors are written with "\n"; these files are CRLF
            nl = "\r\n" if "\r\n" in src else "\n"
            o = old.replace("\n", nl) if nl != "\n" else old
            n = new.replace("\n", nl) if nl != "\n" else new
            if src.count(o) != 1:
                print("  REFUSING  %s -- anchor found %dx" % (label, src.count(o)))
                continue
            try:
                write(path, src.replace(o, n, 1))
                failed = not run_suite()
            finally:
                write(path, src)
            caught += failed
            print("  %s  %s" % ("CAUGHT " if failed else "MISSED ", label))
    finally:
        for p, src in originals.items():
            write(p, src)
        for p, h in hashes.items():
            print("restored %s: %s" % (os.path.basename(p),
                                       "identical" if md5(p) == h else "*** DIFFERS ***"))
    print("\n%d/%d caught" % (caught, len(SABOTAGES)))
    return 0 if caught == len(SABOTAGES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
