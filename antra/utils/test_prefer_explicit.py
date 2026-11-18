"""Issue #61 - "prefer explicit versions" downloaded the clean version anyway.

Two halves, because the bug had two halves:

  1. PARSING - every Spotify fetcher except _parse_track dropped the explicit
     flag, so TrackMetadata.is_explicit was always None. Measured 2026-09-24
     against the live API: Drake's Scorpion (every track explicit) came back
     25/25 None from both the partner GraphQL path and _fetch_album.

  2. WIRING - the resolver's four explicit guards are all gated on
     `track.is_explicit is True`, so a None silently turns the whole setting
     off. These tests pin that a KNOWN-explicit track now beats the clean
     candidate, and that an unknown one still stays neutral.

The payload samples below are verbatim shapes captured from the live endpoints
on 2026-09-24 (see the issue write-up in CLAUDE.md).

Run:  python antra/utils/test_prefer_explicit.py
Negative control: antra/utils/test_prefer_explicit_negctl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver
from antra.core.spotify import _explicit_from_itunes, _parse_explicit

FAILURES: list[str] = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    return got == want


# ── 1. parsing: the four shapes Spotify actually answers with ───────────────

PAYLOADS = [
    # (label, payload, expected)
    # Web API (spotipy album_tracks item).
    ("web api explicit", {"name": "Nonstop", "explicit": True}, True),
    ("web api clean", {"name": "Come Together", "explicit": False}, False),
    # Partner GraphQL (album + playlist item shape).
    ("graphql EXPLICIT", {"name": "Survival", "contentRating": {"label": "EXPLICIT"}}, True),
    ("graphql NONE", {"name": "Something", "contentRating": {"label": "NONE"}}, False),
    # Embed __NEXT_DATA__ (album / artist / playlist trackList item).
    ("embed isExplicit true",
     {"title": "God's Plan", "isExplicit": True,
      "contentRatings": {"labels": ["EXPLICIT"]}}, True),
    ("embed isExplicit false",
     {"title": "Here Comes The Sun", "isExplicit": False,
      "contentRatings": {"labels": []}}, False),
    # contentRatings alone, when isExplicit is absent.
    ("embed labels only", {"title": "x", "contentRatings": {"labels": ["EXPLICIT"]}}, True),
    ("embed labels 19+ only",
     {"title": "x", "contentRatings": {"labels": ["NINETEEN_PLUS"]}}, False),
    # Silence must stay silence - None is neutral to the resolver, and
    # guessing "clean" here would re-break the setting in the other direction.
    ("no explicit info", {"name": "x", "duration": 1000}, None),
    ("empty labels are ambiguous", {"name": "x", "contentRatings": {"labels": []}}, None),
    ("not a dict", "nonsense", None),
]

for label, payload, expected in PAYLOADS:
    check(f"_parse_explicit({label})", _parse_explicit(payload), expected)

ITUNES = [
    ("explicit", True),
    ("cleaned", False),
    ("notExplicit", False),
    ("", None),
]
for value, expected in ITUNES:
    check(f"_explicit_from_itunes({value!r})",
          _explicit_from_itunes({"trackExplicitness": value} if value else {}), expected)


# ── 2. wiring: does the resolver actually act on it? ────────────────────────

def _result(title, is_explicit, similarity=0.95):
    return SearchResult(
        source="test",
        title=title,
        artists=["Drake"],
        album="Scorpion",
        duration_ms=210000,
        audio_format=AudioFormat.FLAC,
        quality_kbps=None,
        is_lossless=True,
        # tier 5 (24-bit/96k) on purpose: without it the result is never
        # accepted immediately for ANY reason, and the guard below would
        # pass vacuously. This isolates "clean" as the only reason.
        bit_depth=24,
        sample_rate_hz=96000,
        download_url="https://example.invalid/x",
        stream_id="x",
        similarity_score=similarity,
        is_explicit=is_explicit,
    )


def _resolver(prefer_explicit=True):
    return SourceResolver([], prefer_explicit=prefer_explicit)


EXPLICIT_TRACK = TrackMetadata(title="Nonstop", artists=["Drake"], album="Scorpion", is_explicit=True)
UNKNOWN_TRACK = TrackMetadata(title="Nonstop", artists=["Drake"], album="Scorpion", is_explicit=None)
CLEAN_RESULT = _result("Nonstop", False)
EXPLICIT_RESULT = _result("Nonstop", True)

r = _resolver()

# The penalty is the whole mechanism: a confirmed-clean result must score
# strictly below the same-quality explicit one for a known-explicit track.
clean_pen = r._explicit_penalty(EXPLICIT_TRACK, CLEAN_RESULT)
expl_pen = r._explicit_penalty(EXPLICIT_TRACK, EXPLICIT_RESULT)
check("clean result is penalised for a known-explicit track", clean_pen < 0, True)
check("explicit result is not penalised", expl_pen, 0.0)
check("explicit outscores clean",
      EXPLICIT_RESULT.similarity_score + expl_pen > CLEAN_RESULT.similarity_score + clean_pen,
      True)

# And the hard guards.
check("clean result is flagged as clean", r._result_looks_clean(EXPLICIT_TRACK, CLEAN_RESULT), True)
check("explicit result is not flagged", r._result_looks_clean(EXPLICIT_TRACK, EXPLICIT_RESULT), False)
# Control first: the SAME result minus the clean flag must be accepted, so a
# False below means "rejected for being clean", not "rejected for some other reason".
check("explicit result IS accepted immediately",
      r._accepts_result_immediately(EXPLICIT_RESULT, None, EXPLICIT_TRACK), True)
check("clean result is NOT accepted immediately",
      r._accepts_result_immediately(CLEAN_RESULT, None, EXPLICIT_TRACK), False)
check("unknown-explicit track: the same clean result IS accepted (the reported bug)",
      r._accepts_result_immediately(CLEAN_RESULT, None, UNKNOWN_TRACK), True)

# This is what was broken end to end: with is_explicit unknown - which is what
# every Spotify fetcher produced before the fix - none of the above fires.
check("unknown-explicit track: clean result is NOT penalised",
      r._explicit_penalty(UNKNOWN_TRACK, CLEAN_RESULT), 0.0)
check("unknown-explicit track: clean result is NOT flagged",
      r._result_looks_clean(UNKNOWN_TRACK, CLEAN_RESULT), False)

# Setting off => neutral again, whatever the metadata says.
off = _resolver(prefer_explicit=False)
check("prefer_explicit off: no penalty", off._explicit_penalty(EXPLICIT_TRACK, CLEAN_RESULT), 0.0)
check("prefer_explicit off: not flagged", off._result_looks_clean(EXPLICIT_TRACK, CLEAN_RESULT), False)

# A track that is genuinely clean has no explicit version to prefer.
clean_track = TrackMetadata(title="Here Comes The Sun", artists=["The Beatles"], album="Abbey Road", is_explicit=False)
check("clean track: matching clean result is not penalised",
      r._explicit_penalty(clean_track, _result("Here Comes The Sun", False)), 0.0)


# ── report ─────────────────────────────────────────────────────────────────

total = len(PAYLOADS) + len(ITUNES) + 14
if FAILURES:
    print(f"FAIL - {len(FAILURES)} of {total} checks failed:")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"PASS - {total} checks (parsing + resolver wiring)")
