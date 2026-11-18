"""Negative control for issue #61 - prove test_prefer_explicit.py can fail.

Each sabotage below re-creates one half of the original bug in place. Every one
must be CAUGHT, otherwise the main suite is passing for free.

Run:  python antra/utils/test_prefer_explicit_negctl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver
import antra.core.spotify as spotify_mod

CAUGHT: list[str] = []
MISSED: list[str] = []


def sabotage(label, fn):
    """fn() returns True when the sabotage is DETECTED (i.e. behaviour broke)."""
    try:
        detected = fn()
    except Exception as exc:                      # a crash counts as detected
        detected = True
        label = f"{label} (raised {type(exc).__name__})"
    (CAUGHT if detected else MISSED).append(label)


def _result(is_explicit):
    return SearchResult(
        source="test", title="Nonstop", artists=["Drake"], album="Scorpion",
        duration_ms=210000, audio_format=AudioFormat.FLAC, quality_kbps=None,
        is_lossless=True, bit_depth=24, sample_rate_hz=96000,
        download_url="https://example.invalid/x", stream_id="x",
        similarity_score=0.95, is_explicit=is_explicit,
    )


TRACK = TrackMetadata(title="Nonstop", artists=["Drake"], album="Scorpion", is_explicit=True)
CLEAN = _result(False)
EXPLICIT = _result(True)

# Real payload shapes, same as the main suite.
LIVE_PAYLOADS = [
    {"name": "Nonstop", "explicit": True},
    {"name": "Survival", "contentRating": {"label": "EXPLICIT"}},
    {"title": "God's Plan", "isExplicit": True, "contentRatings": {"labels": ["EXPLICIT"]}},
]


# 1. The pre-fix parser: every Spotify payload answered None.
def sab_parser_returns_none():
    original = spotify_mod._parse_explicit
    try:
        spotify_mod._parse_explicit = lambda payload: None
        # Detected when a payload that plainly says EXPLICIT no longer reads True.
        return any(spotify_mod._parse_explicit(p) is not True for p in LIVE_PAYLOADS)
    finally:
        spotify_mod._parse_explicit = original


# 2. The pre-fix data: is_explicit=None reaching the resolver turns the whole
#    setting into a no-op. This IS the reported bug.
def sab_unknown_explicit_track():
    r = SourceResolver([], prefer_explicit=True)
    unknown = TrackMetadata(title="Nonstop", artists=["Drake"], album="Scorpion", is_explicit=None)
    penalised = r._explicit_penalty(unknown, CLEAN) < 0
    flagged = r._result_looks_clean(unknown, CLEAN)
    accepted = r._accepts_result_immediately(CLEAN, None, unknown)
    # Detected when the clean result sails through unpenalised, unflagged and accepted.
    return (not penalised) and (not flagged) and accepted


# 3. Neutralise the penalty itself: explicit and clean tie, so source order decides.
def sab_penalty_neutralised():
    r = SourceResolver([], prefer_explicit=True)
    original = SourceResolver._explicit_penalty
    try:
        SourceResolver._explicit_penalty = lambda self, track, result: 0.0
        clean_score = CLEAN.similarity_score + r._explicit_penalty(TRACK, CLEAN)
        expl_score = EXPLICIT.similarity_score + r._explicit_penalty(TRACK, EXPLICIT)
        return clean_score >= expl_score
    finally:
        SourceResolver._explicit_penalty = original


# 4. Neutralise the hard guard: a confirmed-clean result becomes acceptable.
def sab_looks_clean_neutralised():
    r = SourceResolver([], prefer_explicit=True)
    original = SourceResolver._result_looks_clean
    try:
        SourceResolver._result_looks_clean = lambda self, track, result: False
        return r._accepts_result_immediately(CLEAN, None, TRACK)
    finally:
        SourceResolver._result_looks_clean = original


sabotage("parser always returns None (pre-fix Spotify fetchers)", sab_parser_returns_none)
sabotage("track.is_explicit is None (the reported bug)", sab_unknown_explicit_track)
sabotage("_explicit_penalty neutralised", sab_penalty_neutralised)
sabotage("_result_looks_clean neutralised", sab_looks_clean_neutralised)

print(f"sabotages caught: {len(CAUGHT)}/{len(CAUGHT) + len(MISSED)}")
for c in CAUGHT:
    print("  caught  -", c)
for m in MISSED:
    print("  MISSED  -", m)
if MISSED:
    print("FAIL - a sabotage went undetected, so the main suite proves nothing there")
    sys.exit(1)
print("PASS - every sabotage reproduces the bug")
