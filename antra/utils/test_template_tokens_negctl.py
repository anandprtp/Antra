"""Negative control for test_template_tokens.py — each sabotage must turn it red.

Files are restored byte-identically after every run (asserted).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFS = ROOT / "antra_shared" / "filename_prefs.py"
ORG = ROOT / "antra" / "utils" / "organizer.py"
UI = ROOT / "antra-wails" / "frontend" / "src" / "App.svelte"
SUITE = ROOT / "antra" / "utils" / "test_template_tokens.py"

SABOTAGES = [
    ("date passed through raw (timestamp not cut)", PREFS,
     '    if match:\n        return "-".join(part for part in match.groups() if part)\n',
     '    if raw:\n        return raw\n'),
    ("unknown explicit rendered as Clean", PREFS,
     '("Clean" if explicit is False else "")', '"Clean"'),
    ("featured guessed from list position", PREFS,
     "    main = [a for a in tx.artists if _norm_name(a) not in feat_keys]\n",
     "    main = tx.artists[:1]\n    featured = featured or tx.artists[1:]\n"),
    ("track_id ignores source_service", PREFS,
     "    attr = _TRACK_ID_BY_SERVICE.get(service)\n", "    attr = None\n"),
    ("position not padded", PREFS,
     "str(position).zfill(width) if position", "str(position) if position"),
    ("{today} not pinned by organizer", ORG,
     b"        self.filename_preferences[RUN_DATE_KEY] = _date.today().isoformat()\r\n".decode(), ""),
    ("token dropped from registry", PREFS, '    "owner",\n    "main_artists",', '    "main_artists",'),
    ("UI chip missing", UI, "'playlist', 'position', 'owner'", "'playlist', 'position'"),
    ("an old token's value changed", PREFS,
     '"isrc": str(getattr(track, "isrc", "") or ""),', '"isrc": str(getattr(track, "isrc", "") or "").lower(),'),
]


def run_suite() -> bool:
    return subprocess.run([sys.executable, str(SUITE)], capture_output=True).returncode == 0


assert run_suite(), "suite must be green before sabotage"
detected = 0
for label, path, old, new in SABOTAGES:
    original = path.read_bytes()
    text = original.decode("utf-8")
    assert text.count(old) == 1, f"anchor for '{label}' matched {text.count(old)} times"
    try:
        path.write_bytes(text.replace(old, new).encode("utf-8"))
        red = not run_suite()
    finally:
        path.write_bytes(original)
    assert path.read_bytes() == original
    detected += red
    print(f"  {'DETECTED' if red else 'MISSED  '}  {label}")

assert run_suite(), "suite must be green after restore"
print(f"\n{detected}/{len(SABOTAGES)} detected")
sys.exit(0 if detected == len(SABOTAGES) else 1)
