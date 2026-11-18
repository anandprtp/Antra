"""Negative control for test_disc_folders.py — every sabotage must turn the suite red.

Each sabotage edits one file in place, runs the suite, and restores the exact
original bytes (verified) before the next one.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "antra" / "utils" / "test_disc_folders.py"

SABOTAGES = [
    ("folder mode keeps a 2-05 disc prefix in the filename", "antra_shared/filename_prefs.py",
     '    if mode in ("track_only", "folder"):\n        return base\n',
     '    if mode == "track_only":\n        return base\n    if mode == "folder" and disc_number:\n        return f"{disc_number}-{base}"\n'),
    ("single-disc albums get a lone CD1/", "antra_shared/filename_prefs.py",
     "    if total <= 1 and disc < 2:\n        return None\n", ""),
    ("no flat-library-root guard", "antra/utils/organizer.py",
     "        if self.flat_library_root:\r\n            return album_folder\r\n", ""),
    ("release_folder_for never strips CD<n>", "antra/utils/organizer.py",
     "            parent = parent.parent\r\n        return str(parent)\r\n",
     "            pass\r\n        return str(parent)\r\n"),
    ("release_folder_for strips in every mode", "antra/utils/organizer.py",
     'str(self.filename_preferences.get("multi_disc_handling") or "").lower() == "folder"', "True"),
    ("single-track path skips the disc folder", "antra/utils/organizer.py",
     "            folder = self._with_disc_folder(self._album_folder(track), track)\r\n",
     "            folder = self._album_folder(track)\r\n"),
    ("engine cover call drops folder=", "antra/core/engine.py",
     "                        file_path, track, folder=self.organizer.release_folder_for(file_path)\r\n",
     "                        file_path, track\r\n"),
    # The label went through $t() in SF-3. Anchoring on the literal English text
    # left this control silently DEAD — it printed "refusing" rather than
    # failing, so 7/8 read as a pass.
    ("UI option missing", "antra-wails/frontend/src/App.svelte",
     "                <option value=\"folder\">{$t('folder.cd1_cd2_subfolders')}</option>\r\n", ""),
]

detected = 0
for label, rel, old, new in SABOTAGES:
    p = ROOT / rel
    original = p.read_bytes()
    text = original.decode("utf-8")
    if text.count(old) != 1:
        print(f"  ANCHOR  {label}: found {text.count(old)}x — refusing")
        continue
    try:
        p.write_bytes(text.replace(old, new).encode("utf-8"))
        r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True, timeout=600)
        caught = r.returncode != 0
    finally:
        p.write_bytes(original)
        assert p.read_bytes() == original, rel
    detected += caught
    print(f"  {'DETECTED' if caught else 'MISSED  '}  {label}")

print(f"\n{detected}/{len(SABOTAGES)} detected")
clean = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True, timeout=600)
print("control run after restore:", "green" if clean.returncode == 0 else "RED")
sys.exit(0 if detected == len(SABOTAGES) and clean.returncode == 0 else 1)
