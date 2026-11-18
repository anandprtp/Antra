"""BUG-MIX-1 - a title's mix/edit designator is part of the recording's identity.

Every pair here is real: taken from the reported playlist
(open.spotify.com/playlist/20Ig9q7Hcdoy8LeS0mPmkt) and from the live catalogue
titles the five mirrors returned for those tracks on 2026-09-24.

Run:  python antra/utils/test_mix_designator.py
Negative control: revert antra/utils/matching.py and every WRONG_MIX case fails.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.resolver import ACCEPT_THRESHOLD, LOSSLESS_ACCEPT_THRESHOLD
from antra.utils.matching import (
    VERSION_MISMATCH_CAP,
    mix_designator,
    score_similarity,
    version_mismatch,
)

# (query_title, query_artist, result_title, result_artist)
WRONG_MIX = [
    # The whole point: a mix name in the suffix, absent from the candidate.
    ("My Feeling (Daddy'S Prime Time Edit)", "Junior Jack", "My Feeling", "Junior Jack"),
    ("Can't Get Enough! - Vocal Club Mix", "Soulsearcher", "Can't Get Enough!", "Soulsearcher"),
    ("Passion - Naked Edit", "Gat Decor", "Passion", "Gat Decor"),
    # Two different mixes of the same song by the same artist.
    ("Need To Feel Loved - 12'' Club Mix", "Reflekt",
     "Need To Feel Loved (Adam K & Soha Vocal Mix)", "Reflekt"),
    ("Take Me Away (Into The Night) - Vocal Radio Mix", "4 Strings",
     "Take Me Away (Into The Night) [Extended Mix]", "4 Strings"),
    ("Missing - Todd Terry Remix", "Everything But The Girl",
     "Missing (Chicane Remix)", "Everything But The Girl"),
    ("U Sure Do (Strike 7\" Mix)", "Strike", "U Sure Do", "Strike"),
    ("On The Beach - Mauro Picotto's Extended CRW Remix", "York",
     "On The Beach (Original Mix)", "York"),
]

RIGHT_MIX = [
    # Same mix, different bracket style - the catalogues disagree constantly.
    ("Can't Get Enough! - Vocal Club Mix", "Soulsearcher",
     "Can't Get Enough! (Vocal Club Mix)", "Soulsearcher"),
    ("Not over Yet - Perfecto Edit", "Grace", "Not Over Yet (Perfecto Edit)", "Grace"),
    ("Turn It Around - Another Mix", "Alena", "Turn It Around (Another Mix)", "Alena"),
    ("Missing - Todd Terry Remix", "Everything But The Girl",
     "Missing (Todd Terry Remix)", "Everything But The Girl"),
    # Qobuz appends a remaster tag to the same mix.
    ('So in Love With You (Full Intention 12"Remix)', "Duke",
     'So in Love With You (Full Intention 12"Remix [Remastered 2021])', "Duke"),
    # Canonical designators: catalogues routinely drop them.
    ("Don't Go - Original Mix", "Awesome 3", "Don't Go", "Awesome 3"),
    ("Mystery Land - Radio Edit", "Y-Traxx", "Mystery Land", "Y-Traxx"),
    ("Le voie le soleil - Original Full", "Subliminal Cuts", "Le voie le soleil", "Subliminal Cuts"),
    # Packaging, not a recording difference.
    ("Good Life (Remastered)", "Inner City", "Good Life", "Inner City"),
    ("Blue Monday", "New Order", "Blue Monday (Deluxe Version)", "New Order"),
    # Credits are strip_collab's job, not the designator's.
    ("Love On My Mind (feat. Amanda Wilson)", "Freemasons", "Love On My Mind", "Freemasons"),
    ("Saltwater", "Chicane", "Saltwater", "Chicane"),
]

# Behaviour that existed before BUG-MIX-1 and must survive it.
PRESERVED = [
    ("Stand Get Up", "Artist", "Stand Get Up (Instrumental)", "Artist", False),
    ("The Boxer", "Simon & Garfunkel", "The Boxer (Live at Central Park)", "Simon & Garfunkel", False),
    ("God's Plan", "Drake", "God's Plan", "Piano Tribute Players", False),
    # This pin existed so that changing the "distinctive title >= 0.90" bypass
    # would be a deliberate decision. BUG-ASIN-1 is that decision: the bypass
    # overrode the wrong-artist cap, and an exact title match is no evidence at
    # all about the artist, so it fired precisely on "different act, same song
    # name". Measured 2026-10-06 on Fastball's "Make Your Mama Proud", where it
    # carried strangers' recordings at 0.642-0.728 -- over FALLBACK_FLOOR (0.60)
    # and, for "Seattle", over ACCEPT_THRESHOLD (0.70). The bypass is gone and
    # this pair now scores WRONG_ARTIST_CAP (0.500) and is REJECTED, matching the
    # web tree. The buried-artist case the bypass was really protecting is
    # handled inside artist_match_score() instead -- see BURIED_ARTIST below.
    ("White Christmas", "Bing Crosby", "White Christmas", "Some Other Singer", False),
]

failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


for qt, qa, rt, ra in WRONG_MIX:
    score = score_similarity(qt, [qa], rt, ra)
    check(version_mismatch(qt, rt), "version_mismatch missed: %r vs %r" % (qt, rt))
    check(score <= VERSION_MISMATCH_CAP,
          "not capped (%.3f > %.2f): %r vs %r" % (score, VERSION_MISMATCH_CAP, qt, rt))
    # The cap has to sit below every acceptance threshold, or capping is theatre.
    check(score < LOSSLESS_ACCEPT_THRESHOLD,
          "accepted in lossless mode (%.3f): %r vs %r" % (score, qt, rt))
    check(score < ACCEPT_THRESHOLD,
          "accepted (%.3f): %r vs %r" % (score, qt, rt))

for qt, qa, rt, ra in RIGHT_MIX:
    score = score_similarity(qt, [qa], rt, ra)
    check(not version_mismatch(qt, rt), "false mismatch: %r vs %r" % (qt, rt))
    check(score >= ACCEPT_THRESHOLD,
          "correct match scored below threshold (%.3f): %r vs %r" % (score, qt, rt))

for qt, qa, rt, ra, want_accept in PRESERVED:
    score = score_similarity(qt, [qa], rt, ra)
    check((score >= ACCEPT_THRESHOLD) == want_accept,
          "pre-existing guard changed (%.3f, wanted accept=%s): %r vs %r"
          % (score, want_accept, qt, rt))

# The designator extractor itself.
check(mix_designator("Can't Get Enough! - Vocal Club Mix") == frozenset({"vocal", "club"}),
      "designator tokens wrong for 'Vocal Club Mix'")
check(mix_designator("Good Life (Remastered)") == frozenset(), "remaster is not a designator")
check(mix_designator("Don't Go - Original Mix") == frozenset(), "'Original Mix' must be canonical")
check(mix_designator("Missing") == frozenset(), "plain title has no designator")
check(mix_designator('Need To Feel Loved - 12" Club Mix')
      == mix_designator("Need To Feel Loved - 12 Inch Club Mix"),
      "inch marks must normalise")

total = len(WRONG_MIX) * 4 + len(RIGHT_MIX) * 2 + len(PRESERVED) + 5
if failures:
    print("FAIL (%d of %d checks)" % (len(failures), total))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK - %d checks passed (%d wrong-mix, %d right-mix, %d preserved)"
      % (total, len(WRONG_MIX), len(RIGHT_MIX), len(PRESERVED)))
