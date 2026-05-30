"""
String similarity helpers for track matching.
"""
import re
from difflib import SequenceMatcher
from typing import Optional


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    text = text.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)   # Remove parenthetical suffixes
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Matches collaboration credits that catalog search engines don't index:
# "(with X)", "[with X]", "(feat. X)", "[feat. X]", "(ft. X)", "(featuring X)", etc.
_COLLAB_CREDIT_RE = re.compile(
    r"\s*[\(\[](with|feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]",
    re.IGNORECASE,
)


def strip_collab(title: str) -> str:
    """Remove collaboration credits from a track title before sending to catalog
    search APIs. Catalogs index tracks under the clean title only — including
    '(with Travis Scott)' or '[feat. Future]' breaks text search.

    Used for search query construction only; raw title is still used for
    similarity scoring so the match is validated against the full title.
    """
    return _COLLAB_CREDIT_RE.sub("", title).strip()


def string_similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity between two strings after normalization."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def score_similarity(
    query_title: str,
    query_artists: list[str],
    result_title: str,
    result_artist: str,
) -> float:
    title_score = string_similarity(query_title, result_title)

    # Artist vs channel name
    artist_score = max(
        (string_similarity(a, result_artist) for a in query_artists),
        default=0.0,
    )

    # Artist name appearing anywhere in the video title (T-Series, Sony etc.)
    title_artist_score = max(
        (string_similarity(a, result_title) for a in query_artists),
        default=0.0,
    )

    best_artist_score = max(artist_score, title_artist_score * 0.8)

    composite = 0.60 * title_score + 0.40 * best_artist_score

    # Fallback: if title alone is a very strong match, don't let
    # a label channel name kill the result
    if title_score >= 0.55 and composite < 0.35:
        return title_score * 0.75

    # Hard cap: if the artist is clearly wrong (score < 0.45), cap the composite
    # below LOSSLESS_ACCEPT_THRESHOLD (0.55) so a perfect title match on a
    # common song title (e.g. "White Christmas") doesn't pull in the wrong artist.
    # Only bypass this when the result has no artist info at all (empty string).
    # Exception: if the title match is very strong (≥ 0.85), the track is
    # distinctive enough to trust the title alone — skip the hard cap.
    if best_artist_score < 0.45 and result_artist.strip():
        if title_score >= 0.90:
            pass  # distinctive title — trust it
        elif title_score >= 0.75:
            return min(composite, 0.55)  # moderate confidence — softer cap
        else:
            return min(composite, 0.50)

    return composite


def duration_close(expected_s: float, actual_s: float, tolerance: int = 10) -> bool:
    """Return True if durations are within an adaptive tolerance window.

    Cross-service catalogs often disagree by a few extra seconds because of
    leading silence, trailing fade-outs, regional edits, or metadata rounding.
    A hard 5-second cutoff is too strict for long tracks and DJ mixes, so we
    keep the caller-provided tolerance as a floor and expand it slightly for
    longer recordings.
    """
    try:
        expected = float(expected_s)
        actual = float(actual_s)
    except (TypeError, ValueError):
        return False

    longer = max(expected, actual)
    adaptive_tolerance = min(30.0, longer * 0.045)
    effective_tolerance = max(float(tolerance), adaptive_tolerance)
    return abs(expected - actual) <= effective_tolerance
