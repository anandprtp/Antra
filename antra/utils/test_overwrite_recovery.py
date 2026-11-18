"""A track set aside for an overwrite must come back if the run was killed.

The desktop Stop button kills the whole process tree, so no `except` handler runs and the
good file stays renamed to *.antra-overwrite-old, invisible to the library.

Run:  python antra/utils/test_overwrite_recovery.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.engine import DownloadEngine

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


SUFFIX = DownloadEngine._OVERWRITE_STASH_SUFFIX


def run(mode, files):
    """files: {relative_name: bytes}; returns (dir listing as {name: bytes}, stash dict)."""
    d = tempfile.mkdtemp(prefix="ow")
    for name, data in files.items():
        open(os.path.join(d, name), "wb").write(data)
    fake = types.SimpleNamespace(
        organizer=types.SimpleNamespace(
            conflict_behavior=mode,
            _existing_file_is_positively_different=lambda track, p: False,
        ),
        _OVERWRITE_STASH_SUFFIX=SUFFIX,
    )
    stash = {}
    DownloadEngine._stash_for_overwrite(fake, object(), os.path.join(d, "01 - Song"), stash)
    return {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d)}, stash


# 1. the bug: killed mid-overwrite left the good file hidden; it is restored (in any mode)
for mode in ("skip", "overwrite"):
    listing, stash = run(mode, {"01 - Song.flac" + SUFFIX: b"GOOD-ORIGINAL"})
    if mode == "skip":
        check("skip mode: hidden original restored", listing.get("01 - Song.flac") == b"GOOD-ORIGINAL")
        check("skip mode: no stale stash left", not any(n.endswith(SUFFIX) for n in listing))
    else:
        # overwrite mode restores it and then sets it aside again for the NEW download
        check("overwrite mode: original not lost (restored, then stashed)",
              b"GOOD-ORIGINAL" in listing.values())

# 2. never clobber a file that exists: both present -> nothing is overwritten by the recovery
listing, stash = run("skip", {"01 - Song.flac": b"NEWER", "01 - Song.flac" + SUFFIX: b"OLD-BACKUP"})
check("existing file is not clobbered by recovery", listing.get("01 - Song.flac") == b"NEWER")
check("backup kept when the original exists", listing.get("01 - Song.flac" + SUFFIX) == b"OLD-BACKUP")

# 3. controls: nothing stale -> nothing changes; overwrite mode still stashes a real original
listing, stash = run("skip", {"01 - Song.flac": b"KEEP"})
check("control: untouched in skip mode", listing == {"01 - Song.flac": b"KEEP"} and not stash)
listing, stash = run("overwrite", {"01 - Song.flac": b"ORIG"})
check("control: overwrite mode still sets the original aside",
      "01 - Song.flac" not in listing and listing.get("01 - Song.flac" + SUFFIX) == b"ORIG" and len(stash) == 1)

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
