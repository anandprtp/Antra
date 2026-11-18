"""AUDIT-DESKTOP-7 — conflict resolution must see existing files past MAX_PATH.

`_resolve_conflict_path` checked existence through `extended_path` in only one
of its three branches. With `append_counter` (and in the counter loops) a plain
`.exists()` is False for an existing file on a >260-char Windows path, so the
"keep both" mode returned the occupied name and the new download overwrote it.

Real files on disk; Windows-only (extended-length prefix behaviour), a no-op elsewhere.
Run:  python antra/utils/test_conflict_longpath.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if os.name != "nt":
    print("SKIP (Windows-only behaviour)")
    sys.exit(0)

org_mod = importlib.import_module("antra.utils.organizer")
lp = importlib.import_module("antra.utils.longpath")

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


base = Path(tempfile.mkdtemp(prefix="lp_"))
folder = base / ("d" * 100) / ("e" * 100)
stem = "s" * 60
assert len(str(folder / f"{stem}.flac")) > 260
os.makedirs(lp.extended_path(str(folder)), exist_ok=True)


def touch(name):
    with open(lp.extended_path(str(folder / name)), "wb") as fh:
        fh.write(b"x")


org = org_mod.LibraryOrganizer(str(base), filename_conflict_behavior="append_counter")
check("control: nothing there -> plain stem", org._resolve_conflict_path(folder, stem).name == stem)

touch(f"{stem}.flac")
got = org._resolve_conflict_path(folder, stem).name
check("append_counter: existing long-path file -> numbered copy", got == f"{stem} (2)", got)

touch(f"{stem} (2).flac")
got = org._resolve_conflict_path(folder, stem).name
check("append_counter: counter loop sees (2) too", got == f"{stem} (3)", got)

shutil.rmtree(lp.extended_path(str(base)), ignore_errors=True)
print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
