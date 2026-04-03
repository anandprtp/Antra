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

    return composite


def duration_close(expected_s: float, actual_s: float, tolerance: int = 10) -> bool:
    """Return True if durations are within `tolerance` seconds of each other."""
    return abs(expected_s - actual_s) <= tolerance
