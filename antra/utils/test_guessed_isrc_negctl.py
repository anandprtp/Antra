"""Negative control for test_guessed_isrc.py.

Sabotages each fix in place, one at a time, and asserts the suite's checks
actually notice. Without this, "13 checks passed" could mean the checks never
depended on the change.

Run:  python antra/utils/test_guessed_isrc_negctl.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core import isrc_enricher
from antra.core.models import SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver
from antra.core.service import AntraService

missed = []


def sabotage(label, cond):
    """cond must be True: the sabotaged code must produce the OLD, wrong answer."""
    if not cond:
        missed.append(label)


# 1. The old bar (0.65, no duration, no designator check) accepts the wrong mix.
#    Scored with the designator comparison disabled, i.e. exactly as the shipped
#    website code scored it.
import antra.utils.matching as matching

_real_designator = matching.mix_designator
try:
    matching.mix_designator = lambda title: frozenset()
    old_bar_accepts = matching.score_similarity(
        "Can't Get Enough! - Vocal Club Mix", ["Soulsearcher"],
        "Can't Get Enough!", "Soulsearcher",
    ) >= 0.65
finally:
    matching.mix_designator = _real_designator
sabotage("old 0.65 bar no longer accepts the wrong mix - check the fixture",
         old_bar_accepts)

# ...and the new bar rejects exactly that case.
sabotage("new bar accepts the wrong mix",
         not isrc_enricher.guessed_isrc_is_acceptable(
             "Can't Get Enough! - Vocal Club Mix", ["Soulsearcher"], 412973,
             "Can't Get Enough!", "Soulsearcher", 214))

# 2. Drop the duration requirement -> a wrong-length hit is accepted again.
_real_close = None
try:
    _real_close = matching.duration_close
    matching.duration_close = lambda a, b, tolerance=10: True
    sabotage("removing the duration check changed nothing - it is not load-bearing",
             isrc_enricher.guessed_isrc_is_acceptable(
                 "Saltwater", ["Chicane"], 202533, "Saltwater", "Chicane", 402))
finally:
    if _real_close is not None:
        matching.duration_close = _real_close

# 3. Ignore isrc_guessed in the gate -> the wrong mix passes again.
resolver = SourceResolver([])
track = TrackMetadata(title="Can't Get Enough! - Vocal Club Mix",
                      artists=["Soulsearcher"], album="Album")
track.duration_ms = 412973
track.isrc = "GBCPZ9900001"
track.isrc_guessed = True
wrong = SearchResult(
    source="tidal_mirror", title="Can't Get Enough!", artists=["Soulsearcher"],
    album="Album", duration_ms=412000, audio_format="FLAC", quality_kbps=1411,
    is_lossless=True, download_url=None, stream_id="1",
    similarity_score=1.0, isrc_match=True,
)
sabotage("the gate rejects the wrong mix even with isrc_guessed cleared - "
         "the flag is not what is doing the work",
         resolver._passes_identity_gate(
             types.SimpleNamespace(**{**track.__dict__, "isrc_guessed": False}), wrong) is True)

# 4. Restore the old blanket cap -> ISRC enrichment is skipped entirely.
service = AntraService()
called = {"n": 0}
service._enrich_isrcs = lambda tracks: called.__setitem__("n", len(tracks))
service.build_runtime_config = lambda options=None: types.SimpleNamespace(enrich_album_data=False)
service.validate_config = lambda cfg: None
big = []
for i in range(250):
    t = TrackMetadata(title="Track %d" % i, artists=["A"], album="Album")
    big.append(t)
service.enrich_tracks_for_download(big, "https://open.spotify.com/playlist/x")
sabotage("enrichment ran with enrich_album_data=False", called["n"] == 0)

if missed:
    print("CONTROL FAILED (%d):" % len(missed))
    for m in missed:
        print("  - " + m)
    sys.exit(1)
print("OK - control confirms all 4 sabotages reproduce the old behaviour")
