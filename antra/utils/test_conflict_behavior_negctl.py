"""Negative control for test_conflict_behavior.py (v2.0.0 DMX-5).

Copies the tree, applies one sabotage at a time, and asserts the suite goes RED.
A suite that stays green against broken code proves nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "antra/utils/test_conflict_behavior.py"

SABOTAGES = [
    ("overwrite never stashes the original",
     "antra/core/engine.py",
     "            self._stash_for_overwrite(track, output_base, overwrite_stash)",
     "            pass"),
    ("a failed overwrite discards the backup instead of restoring",
     "antra/core/engine.py",
     "            self._finish_overwrite_stash(stash, success=False)",
     "            self._finish_overwrite_stash(stash, success=True)"),
    ("tag_only re-tags a copy filed under another release",
     "antra/utils/organizer.py",
     "        if found_base != target_base:",
     "        if False:"),
    ("tag_only stops deduplicating like skip",
     "antra/utils/organizer.py",
     '        if self.conflict_behavior in ("skip", "tag_only") and not self.full_albums:',
     '        if self.conflict_behavior == "skip" and not self.full_albums:'),
    ("a re-tag stamps provenance",
     "antra/core/engine.py",
     "        ok = self.tagger.tag(path, track, provenance=False)",
     "        ok = self.tagger.tag(path, track)"),
    ("tag_only never actually re-tags",
     "antra/core/engine.py",
     "                    retagged = self._retag_existing(track, retag_path)",
     "                    retagged = False"),
    ("hard-linked playlist copies get re-tagged",
     "antra/utils/organizer.py",
     "            if track.playlist_name and os.stat(extended_path(str(found))).st_nlink > 1:",
     "            if False:"),
    ("aliases dropped from the normaliser",
     "antra_shared/filename_prefs.py",
     '    "keep_both": "append_counter",',
     ""),
]


def run_suite(tree: Path) -> bool:
    proc = subprocess.run([sys.executable, str(tree / SUITE), str(tree)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0


def copy_tree() -> Path:
    base = Path(tempfile.mkdtemp(prefix="dmx5_neg_"))
    for pkg in ("antra", "antra_shared"):
        shutil.copytree(ROOT / pkg, base / pkg, ignore=shutil.ignore_patterns("*.bak", "__pycache__"))
    (base / "antra-wails/frontend/src/lib").mkdir(parents=True)
    shutil.copy(ROOT / "antra-wails/frontend/src/App.svelte", base / "antra-wails/frontend/src/App.svelte")
    shutil.copy(ROOT / "antra-wails/frontend/src/lib/settings-catalog.ts",
                base / "antra-wails/frontend/src/lib/settings-catalog.ts")
    shutil.copy(ROOT / "antra-wails/app_backend.go", base / "antra-wails/app_backend.go")
    return base


control = copy_tree()
if not run_suite(control):
    print("ABORT: control tree is not green")
    sys.exit(2)
shutil.rmtree(control, ignore_errors=True)
print("control: green")

missed = 0
for label, rel, old, new in SABOTAGES:
    tree = copy_tree()
    p = tree / rel
    data = p.read_bytes().decode("utf-8")
    if data.count(old) != 1:
        print(f"  ABORT sabotage anchor not unique ({data.count(old)}): {label}")
        sys.exit(2)
    p.write_bytes(data.replace(old, new).encode("utf-8"))
    green = run_suite(tree)
    print(f"  {'MISSED  ' if green else 'detected'}  {label}")
    missed += int(green)
    shutil.rmtree(tree, ignore_errors=True)

print(f"\n{len(SABOTAGES) - missed}/{len(SABOTAGES)} detected")
sys.exit(1 if missed else 0)
