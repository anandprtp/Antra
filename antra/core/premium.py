"""Session-level cache for Gist-fetched premium server endpoints."""
import logging

import requests

logger = logging.getLogger(__name__)

_GIST_CACHE: dict | None = None


def fetch_premium_endpoints(gist_url: str) -> dict:
    """
    Fetch premium server endpoint JSON from a raw Gist URL (one-shot, session-cached).

    Expected Gist format:
        {"amazon": "https://amazon.example.com", "tidal": "https://tidal.example.com"}

    Returns {} on any failure so callers can treat it as "no premium servers available".
    The URL is the only value that may be hardcoded; server URLs are never stored locally.
    """
    global _GIST_CACHE
    if _GIST_CACHE is not None:
        return _GIST_CACHE
    if not gist_url or gist_url == "PLACEHOLDER":
        _GIST_CACHE = {}
        return {}
    try:
        resp = requests.get(gist_url, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _GIST_CACHE = {k: v.rstrip("/") for k, v in data.items() if isinstance(v, str)}
                logger.debug("[Premium] Endpoint manifest loaded.")
                return _GIST_CACHE
    except Exception:
        pass
    _GIST_CACHE = {}
    return {}
