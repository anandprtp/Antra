"""Negative control for PORT-2.

Breaks the SHIPPED `antra/utils/sidecars.py` one way at a time and asserts the
suite goes red each time. A test that cannot fail is worse than no test, and
this project's own history has two recorded cases of a sabotage being MISSED
because an assertion was satisfied for the wrong reason.

Every sabotage is reverted and the file is restored byte-for-byte; the suite is
run green before and after so a botched restore cannot go unnoticed.

Run:  python antra/utils/test_sidecars_negctl.py
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "antra" / "utils" / "sidecars.py"
SUITE = ROOT / "antra" / "utils" / "test_sidecars.py"


def run_suite() -> bool:
    """True when the suite passes."""
    r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode == 0


# (label, exact substring in the shipped file, replacement)
SABOTAGES = [
    (
        "coverage rule removed (a 1-of-12 run overwrites the good sidecar)",
        "        if len(present) < on_disk:",
        "        if False:",
    ),
    (
        "vanished files still counted as present",
        "present = [(t, p) for t, p in tracks if os.path.exists(extended_path(str(p)))]",
        "present = list(tracks)",
    ),
    (
        "CUE track number no longer clamped to 1-99",
        "        all(n is not None and 1 <= n <= _CUE_MAX_TRACKS for n in reals)",
        "        all(n is not None for n in reals)",
    ),
    (
        "CUE stops truncating at 99 tracks",
        "    shown = tracks[:_CUE_MAX_TRACKS]",
        "    shown = tracks[:]",
    ),
    (
        "CUE double quotes escaped instead of replaced (no such escape exists)",
        'v = (value or "").replace(\'"\', "\'")',
        'v = (value or "").replace(\'"\', \'\\\\"\')',
    ),
    (
        "invalid ISRCs written through unchecked",
        "    return isrc if _ISRC_RE.match(isrc) else None",
        "    return isrc or None",
    ),
    (
        "one cue for all discs instead of one per disc",
        "                discs.setdefault(_disc_of(track), []).append((track, path))",
        "                discs.setdefault(1, []).append((track, path))",
    ),
    (
        "multi-disc .nfo stops qualifying track numbers with the disc",
        "    multi_disc = len({_disc_of(t) for t, _ in tracks}) > 1",
        "    multi_disc = False",
    ),
    (
        "leading dots no longer stripped from the sidecar name",
        'name = name.strip().strip(". ")',
        'name = name.strip().rstrip(". ")',
    ),
    (
        ".cue / .nfo written LF instead of CRLF",
        'newline = "\\r\\n" if plan.kind in _CRLF_KINDS else "\\n"',
        'newline = "\\n"',
    ),
    (
        "a failed write aborts the remaining sidecars",
        '        except (OSError, ValueError) as e:  # ValueError covers UnicodeEncodeError\n            logger.warning("[Sidecars] could not write %s: %s", plan.path, e)',
        "        except (OSError, ValueError):\n            raise",
    ),
    (
        "toggles ignored — sidecars written even with everything off",
        "    if not (want_cue or want_nfo or want_json):\n        return []\n    groups = filter_to_covered",
        "    if False:\n        return []\n    want_nfo = True\n    groups = filter_to_covered",
    ),
]


def main() -> int:
    original = TARGET.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    # sidecars.py is pure CRLF (it matches its 19 siblings in antra/utils).
    # The anchors below are written with LF, so match against an LF view and
    # write the sabotage back as CRLF - otherwise every multi-line anchor
    # silently matches ZERO times and the control reports a MISS that is
    # really a line-ending mistake. That is what the first run of this file
    # did: 3 of 12 "misses" were the anchors, not the code.
    LF = chr(10)
    CRLF = chr(13) + chr(10)
    text = original.decode("utf-8").replace(CRLF, LF)
    is_crlf = CRLF.encode("utf-8") in original

    def write_sabotaged(body: str) -> None:
        out = body.replace(LF, CRLF) if is_crlf else body
        TARGET.write_bytes(out.encode("utf-8"))

    print("Control run BEFORE sabotage:", "green" if run_suite() else "RED (abort)")
    if not run_suite():
        return 1

    caught = 0
    missed = []
    for label, old, new in SABOTAGES:
        if text.count(old) != 1:
            print(f"  SKIP    {label}  --> anchor matched {text.count(old)} times")
            missed.append(label + " (anchor)")
            continue
        write_sabotaged(text.replace(old, new, 1))
        try:
            ok = run_suite()
        finally:
            TARGET.write_bytes(original)
        if ok:
            print(f"  MISSED  {label}")
            missed.append(label)
        else:
            print(f"  caught  {label}")
            caught += 1

    restored_ok = hashlib.sha256(TARGET.read_bytes()).hexdigest() == digest
    print(f"\nControl: {caught}/{len(SABOTAGES)} detected")
    print("File restored byte-identically:", "yes" if restored_ok else "NO")
    print("Suite green again after restore:", "yes" if run_suite() else "no")
    if missed:
        print("MISSED:", "; ".join(missed))
    return 0 if (caught == len(SABOTAGES) and restored_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
