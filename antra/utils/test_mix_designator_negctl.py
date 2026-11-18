"""Negative control for test_mix_designator.py.

Disables ONLY the new designator comparison (mix_designator always returns the
empty set, which is exactly the pre-BUG-MIX-1 behaviour) and re-runs the
wrong-mix table. Every case must now sail through, proving the main suite's
assertions are carried by the new logic and not by something already present.

Run:  python antra/utils/test_mix_designator_negctl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.resolver import ACCEPT_THRESHOLD
from antra.utils import matching

WRONG_MIX = [
    ("My Feeling (Daddy'S Prime Time Edit)", "Junior Jack", "My Feeling", "Junior Jack"),
    ("Can't Get Enough! - Vocal Club Mix", "Soulsearcher", "Can't Get Enough!", "Soulsearcher"),
    ("Passion - Naked Edit", "Gat Decor", "Passion", "Gat Decor"),
    ("Need To Feel Loved - 12'' Club Mix", "Reflekt",
     "Need To Feel Loved (Adam K & Soha Vocal Mix)", "Reflekt"),
    ("Take Me Away (Into The Night) - Vocal Radio Mix", "4 Strings",
     "Take Me Away (Into The Night) [Extended Mix]", "4 Strings"),
    ("U Sure Do (Strike 7\" Mix)", "Strike", "U Sure Do", "Strike"),
]

matching.mix_designator = lambda title: frozenset()   # pre-fix behaviour

leaked = []
for qt, qa, rt, ra in WRONG_MIX:
    score = matching.score_similarity(qt, [qa], rt, ra)
    if score >= ACCEPT_THRESHOLD:
        leaked.append((qt, rt, score))

if len(leaked) != len(WRONG_MIX):
    print("CONTROL FAILED - only %d of %d wrong mixes leaked without the fix; "
          "the main suite may be passing for the wrong reason."
          % (len(leaked), len(WRONG_MIX)))
    sys.exit(1)

print("OK - control reproduces the bug: all %d wrong mixes accepted without the "
      "designator check" % len(leaked))
for qt, rt, score in leaked:
    print("  %.3f  %r -> %r" % (score, qt, rt))
