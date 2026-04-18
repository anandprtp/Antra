"""
MusicBrainz genre enricher.

Queries the MusicBrainz recording API by ISRC to fetch community genre tags
and populate TrackMetadata.genres before tagging. Results are cached in-memory
for the session so the same ISRC is never fetched twice.

Rate limiting: MusicBrainz asks for no more than 1 request/second from any
single IP. We enforce a 1.1s minimum interval between requests.
"""
import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_MB_API = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Antra/1.0 (https://github.com/anandprtp/antra)"
_MIN_INTERVAL = 1.1          # seconds between requests (MB policy)
_REQUEST_TIMEOUT = 10        # seconds
_MAX_GENRES = 5              # cap to avoid tag spam
_MIN_TAG_COUNT = 0           # include all tags (MusicBrainz tags are already curated)

_cache: dict[str, list[str]] = {}
_cache_lock = threading.Lock()
_throttle_lock = threading.Lock()
_next_request_at: float = 0.0


def _throttle() -> None:
    """Block until it's safe to make the next request."""
    global _next_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_request_at - now
        _next_request_at = max(now, _next_request_at) + _MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


def fetch_genres(isrc: str) -> list[str]:
    """
    Return a list of genre strings for the given ISRC, or [] if none found.
    Results are cached — repeated calls for the same ISRC are free.
    """
    isrc = isrc.strip().upper()
    if not isrc:
        return []

    with _cache_lock:
        if isrc in _cache:
            return _cache[isrc]

    try:
        _throttle()
        resp = requests.get(
            f"{_MB_API}/recording",
            params={
                "query": f"isrc:{isrc}",
                "fmt": "json",
                "inc": "tags",
                "limit": 5,
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug(f"[MusicBrainz] Request failed for ISRC {isrc}: {e}")
        with _cache_lock:
            _cache[isrc] = []
        return []

    recordings = data.get("recordings") or []
    if not recordings:
        with _cache_lock:
            _cache[isrc] = []
        return []

    # Collect tags from the first (best-match) recording, sorted by vote count
    tags = recordings[0].get("tags") or []
    tags_sorted = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)

    genres = []
    for tag in tags_sorted[:_MAX_GENRES]:
        name = tag.get("name", "").strip()
        if name:
            genres.append(name.title())

    logger.debug(f"[MusicBrainz] ISRC {isrc} → genres: {genres}")
    with _cache_lock:
        _cache[isrc] = genres
    return genres
