"""v2.0.0 SF-2 - negative control for test_replaygain.py.

Each sabotage edits the real module or engine in place, runs the suite, and
restores the file byte-for-byte. Every sabotage must turn the suite red.

    python antra/utils/test_replaygain_negctl.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RG = os.path.join(ROOT, "antra", "utils", "replaygain.py")
ENGINE = os.path.join(ROOT, "antra", "core", "engine.py")
SUITE = os.path.join(ROOT, "antra", "utils", "test_replaygain.py")

SABOTAGES = [
    ("relative gate removed", RG,
     "    kept = [b for b in passed if b > relative]", "    kept = list(passed)"),
    ("gain sign flipped", RG,
     "    return REFERENCE_LUFS - integrated_lufs", "    return integrated_lufs - REFERENCE_LUFS"),
    ("absolute gate made inclusive", RG,
     "b > ABSOLUTE_GATE_LUFS]", "b >= ABSOLUTE_GATE_LUFS]"),
    ("silence gets a loudness", RG,
     "    if not passed:\n        return None", "    if not passed:\n        return -70.0"),
    ("album peak not the max", RG,
     "max(m.peak for m in measurements)", "measurements[0].peak"),
    ("MP3 saved as ID3v2.4", RG,
     "audio.save(path, v1=id3_v1, v2_version=3)", "audio.save(path, v1=id3_v1, v2_version=4)"),
    ("clear_album ignored", RG,
     "    if clear_album and album is None:", "    if False:"),
    ("MP3 case variants duplicated", RG,
     'frame_key[5:].upper() in removes', 'frame_key[5:] in removes'),
    ("completeness check removed", ENGINE,
     "                if incomplete:", "                if False:"),
    # NOT listed: dropping the `r.track.playlist_name` guard in the album
    # grouping. Playlist rows are never stored for the album pass (the guard
    # below), so the pass returns at `if not measured` before grouping - the
    # grouping guard is unreachable defence-in-depth, measured unobservable by
    # this control on 2026-09-13, and listing it would claim a detection that
    # cannot exist. A real batch never mixes playlist and release rows.
    ("partial release accepted", ENGINE,
     "                if not whole:", "                if False:"),
    ("track hook not called", ENGINE,
     "                self._apply_track_replaygain(file_path, track)", "                pass"),
    ("atmos not skipped", ENGINE,
     '        return not (self.cfg.output_format or "").lower().startswith("atmos")', "        return True"),
    ("measurements not consumed", ENGINE,
     "            self._rg_measurements.clear()", "            pass"),
    ("playlist rows kept in memory", ENGINE,
     "            if not track.playlist_name:", "            if True:"),
]


def sha(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main() -> int:
    originals = {p: open(p, "rb").read() for p in {RG, ENGINE}}
    hashes = {p: sha(p) for p in originals}
    base = subprocess.run([sys.executable, SUITE], capture_output=True, text=True)
    if base.returncode != 0:
        print("control run is not green - fix the suite first")
        print(base.stdout[-2000:])
        return 2
    missed = []
    try:
        for label, path, old, new in SABOTAGES:
            raw = originals[path]
            crlf = b"\r\n" in raw
            text = raw.decode("utf-8").replace("\r\n", "\n")
            if text.count(old) != 1:
                print(f"  ANCHOR  {label}: found {text.count(old)}x - refusing")
                missed.append(label)
                continue
            patched = text.replace(old, new)
            if crlf:
                patched = patched.replace("\n", "\r\n")
            open(path, "wb").write(patched.encode("utf-8"))
            try:
                run = subprocess.run([sys.executable, SUITE], capture_output=True, text=True, timeout=600)
            finally:
                open(path, "wb").write(raw)
            if run.returncode == 0:
                print(f"  MISSED  {label}")
                missed.append(label)
            else:
                first = next((ln for ln in run.stdout.splitlines() if "FAIL" in ln or "Error" in ln), "(crashed)")
                print(f"  caught  {label}: {first.strip()[:110]}")
    finally:
        for p, raw in originals.items():
            open(p, "wb").write(raw)
    restored = all(sha(p) == h for p, h in hashes.items())
    control = subprocess.run([sys.executable, SUITE], capture_output=True, text=True).returncode == 0
    print(f"\n{len(SABOTAGES) - len(missed)}/{len(SABOTAGES)} detected; files restored byte-identically: "
          f"{restored}; control green after: {control}")
    return 0 if not missed and restored and control else 1


if __name__ == "__main__":
    sys.exit(main())
