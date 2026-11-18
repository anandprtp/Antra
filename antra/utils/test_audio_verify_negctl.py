"""Negative control for test_audio_verify.

Sabotages audio_verify.py one change at a time, re-runs the suite, and asserts
it goes red. A suite that stays green against broken code proves nothing.
The file is restored byte-identically afterwards (verified by hash).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "audio_verify.py")
SUITE = os.path.join(HERE, "test_audio_verify.py")

# (label, old, new)
SABOTAGES = [
    ("threshold never rejects",
     "    if score > threshold:",
     "    if False and score > threshold:"),
    ("preview duration gate removed",
     "            if not got or abs(got - duration_s) > 3:\n                continue",
     "            if False:\n                continue"),
    ("preview title gate removed",
     "        if want_title not in got_title and got_title not in want_title:\n            continue",
     "        if False:\n            continue"),
    ("fingerprint raises instead of degrading",
     "    exe = get_fpcalc_exe()\n    if not exe:\n        return None",
     "    exe = get_fpcalc_exe()\n    if not exe:\n        raise RuntimeError('no fpcalc')"),
    ("preview temp file never cleaned up",
     "        try:\n            os.unlink(preview_path)\n        except OSError:\n            pass",
     "        pass"),
    ("preview artist gate removed",
     '        if want_artist and want_artist.split(" ")[0] not in _norm(item.get("artistName")):\n            continue',
     "        if False:\n            continue"),
    # NOT sabotaged: the `overlap < _MIN_OVERLAP` guard inside rate(). The offset
    # range already guarantees at least that much overlap, so removing it changes
    # no observable behaviour -- it is kept as defence in case that range changes.
]


def run_suite(roots: list[str]) -> bool:
    """True when the suite passes."""
    proc = subprocess.run([sys.executable, SUITE, *roots], capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    roots = sys.argv[1:] or [os.path.join(os.path.expanduser("~"), "Music")]
    original = open(TARGET, "rb").read()
    digest = hashlib.sha256(original).hexdigest()
    src = original.decode("utf-8")

    if not run_suite(roots):
        print("ABORT: suite is already failing on unmodified code")
        return 2
    print("control: suite passes on unmodified code\n")

    caught = missed = 0
    # The target's line endings are whatever the tree uses (CRLF on desktop).
    # Anchors below are written with "\n", so translate them or every
    # multi-line anchor silently matches nothing and this control measures NOTHING.
    eol = "\r\n" if "\r\n" in src else "\n"

    try:
        for label, old, new in SABOTAGES:
            old = old.replace("\n", eol)
            new = new.replace("\n", eol)
            if src.count(old) != 1:
                # A sabotage that will not apply is broken tooling, not a missed
                # detection -- counting it as "missed" wrongly blames the product.
                print(f"  ABORT  {label}: anchor matched {src.count(old)} times -- control is stale")
                return 2
            open(TARGET, "w", encoding="utf-8", newline="").write(src.replace(old, new, 1))
            ok = run_suite(roots)
            if ok:
                missed += 1
                print(f"  MISSED {label} -- suite still passed")
            else:
                caught += 1
                print(f"  caught {label}")
    finally:
        open(TARGET, "wb").write(original)
        restored = hashlib.sha256(open(TARGET, "rb").read()).hexdigest()
        print(f"\nrestored byte-identically: {restored == digest}")

    print(f"{caught} caught, {missed} missed, of {len(SABOTAGES)}")
    if not run_suite(roots):
        print("ERROR: suite fails after restore")
        return 2
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
