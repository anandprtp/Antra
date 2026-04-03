"""
Link resolver — cross-platform track ID lookup.

Uses a pool of resolvers tried in order, no API key required for any of them:

  1. Amazon Product Search (amazon.com/s) — scrapes digital-music-track search
     results. No rate limits, no auth. Used specifically to find Amazon ASINs.

  2. Songwhip (api.songwhip.com) — slug-based API (api.songwhip.com/v3/resolve) — no rate limits, returns
     Amazon / Tidal / Qobuz / Apple IDs. Only works for tracks already in
     Songwhip's index (popular releases).

  3. Odesli / song.link — Spotify ID / ISRC lookup, most accurate, but
     rate-limited at ~10 req/min without a key. Retried with backoff.

All successful results are persisted to ~/.antra_link_cache.json so each
unique track is only ever looked up once across all runs.

Returns a dict mapping platform name → ID/ASIN string, e.g.:
    {"amazonMusic": "B07XVMPVHD", "tidal": "12345678", "qobuz": "abcdef"}

Never raises — returns {} on total failure.
"""
import json
import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_ODESLI_API = "https://api.song.link/v1-alpha.1/links"
_SONGWHIP_API = "https://api.songwhip.com/v3/resolve"
_AMAZON_SEARCH = "https://www.amazon.com/s"
_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".antra_link_cache.json")
_ODESLI_RETRY_DELAYS = [10, 30, 60]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}


def _load_cache() -> dict:
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _to_slug(s: str) -> str:
    """Convert a string to a Songwhip-style URL slug."""
    s = s.lower()
    s = re.sub(r"[&/\\|]", " ", s)
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s)
    return s


class OdesliEnricher:
    """
    Resolve platform-specific track IDs using a pool of resolvers.

    Pool order (no-key, no-rate-limit sources first):
      1. Amazon product search — finds Amazon ASIN via amazon.com scrape
      2. Songwhip              — finds Amazon/Tidal/Qobuz/Apple IDs via slug
      3. Odesli                — most accurate (Spotify ID/ISRC), rate-limited

    Results are cached to disk so each track is only looked up once ever.
    """

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key
        self._cache = _load_cache()
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ── Public interface ──────────────────────────────────────────────────────

    def resolve(self, track) -> dict[str, str]:
        """
        Return {platform: id} for the given track.
        Merges results from all resolvers, caches on first success.
        """
        cache_key = getattr(track, "spotify_id", None) or getattr(track, "isrc", None)

        if cache_key and cache_key in self._cache:
            logger.debug(f"[LinkResolver] Cache hit for '{track.title}'")
            return self._cache[cache_key]

        result: dict[str, str] = {}

        # 1. Amazon product search (no limits, always try first for amazonMusic)
        amazon_asin = self._search_amazon(track)
        if amazon_asin:
            result["amazonMusic"] = amazon_asin
            logger.debug(f"[LinkResolver] Amazon ASIN via product search: {amazon_asin}")

        # 2. Songwhip (no limits, fills tidal/qobuz/apple if not already found)
        sw = self._try_songwhip(track)
        for k, v in sw.items():
            result.setdefault(k, v)

        # 3. Odesli (rate-limited fallback — only if still missing key IDs)
        if not result:
            od = self._try_odesli(track)
            for k, v in od.items():
                result.setdefault(k, v)

        if result:
            logger.debug(f"[LinkResolver] Resolved '{track.title}': {list(result.keys())}")
            self._store(cache_key, result)
        else:
            logger.debug(f"[LinkResolver] No platform IDs found for '{track.title}'")

        return result

    # ── Amazon product search ─────────────────────────────────────────────────

    def _search_amazon(self, track) -> Optional[str]:
        """
        Search amazon.com/s for the track in the digital-music-track category.
        Extracts the best-matching ASIN using title similarity scoring.
        No API key, no rate limits.
        """
        title = getattr(track, "title", "") or ""
        artists = getattr(track, "artists", []) or []
        artist = artists[0] if artists else ""
        if not title:
            return None

        query = f"{title} {artist}".strip()
        try:
            resp = self._session.get(
                _AMAZON_SEARCH,
                params={"k": query, "i": "digital-music-track"},
                timeout=10,
            )
            if not resp.ok:
                logger.debug(f"[Amazon Search] HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.debug(f"[Amazon Search] Request failed: {e}")
            return None

        # Extract (ASIN, product title) pairs from the result page.
        # Amazon embeds data-asin on product cards alongside h2 > span title text.
        pairs = re.findall(
            r'data-asin="([A-Z0-9]{10})"[^>]*>.*?<h2[^>]*>.*?<span[^>]*>([^<]+)</span>',
            resp.text[:400000],
            re.DOTALL,
        )

        title_lower = title.lower()
        artist_lower = artist.lower()

        for asin, product_title in pairs:
            pt_lower = product_title.strip().lower()
            # Accept if product title contains our track title (case-insensitive)
            if title_lower in pt_lower or pt_lower in title_lower:
                logger.debug(f"[Amazon Search] Matched '{product_title.strip()}' → {asin}")
                return asin

        # Looser fallback: first result (Amazon ranks by relevance)
        if pairs:
            asin, product_title = pairs[0]
            logger.debug(f"[Amazon Search] Using first result '{product_title.strip()}' → {asin}")
            return asin

        return None

    # ── Songwhip ──────────────────────────────────────────────────────────────

    def _try_songwhip(self, track) -> dict[str, str]:
        """
        Fetch from Songwhip's public slug-based API.
        Only works for tracks already indexed by Songwhip (most popular music).
        """
        artist = (getattr(track, "artists", None) or [""])[0]
        title = getattr(track, "title", "") or ""
        if not artist or not title:
            return {}

        artist_slug = _to_slug(artist)
        title_slug = _to_slug(title)
        title_slug_clean = re.sub(r"-feat-.*$|-ft-.*$|-featuring-.*$", "", title_slug)

        for t_slug in dict.fromkeys([title_slug_clean, title_slug]):
            url = f"{_SONGWHIP_API}/{artist_slug}/{t_slug}"
            try:
                resp = self._session.get(url, timeout=8)
                if resp.status_code == 200:
                    return self._extract_songwhip(resp.json())
                logger.debug(f"[Songwhip] {resp.status_code} for {artist_slug}/{t_slug}")
            except Exception as e:
                logger.debug(f"[Songwhip] Request failed: {e}")

        return {}

    def _extract_songwhip(self, data: dict) -> dict[str, str]:
        links = data.get("data", {}).get("links", {})
        result: dict[str, str] = {}

        # Amazon Music — extract trackAsin, prefer US storefront
        for entry in links.get("amazonMusic", []):
            url = entry.get("link", "")
            countries = entry.get("countries")
            match = re.search(r"trackAsin=([A-Z0-9]{10})", url)
            if match:
                asin = match.group(1)
                if countries is None or "US" in countries:
                    result["amazonMusic"] = asin
                    break
                result.setdefault("amazonMusic", asin)

        # Tidal
        for entry in links.get("tidal", []):
            m = re.search(r"/track/(\d+)", entry.get("link", ""))
            if m:
                result["tidal"] = m.group(1)
                break

        # Qobuz
        for entry in links.get("qobuz", []):
            m = re.search(r"/track/(\d+)", entry.get("link", ""))
            if m:
                result["qobuz"] = m.group(1)
                break

        # Apple Music
        for entry in links.get("itunes", []):
            m = re.search(r"[?&]i=(\d+)", entry.get("link", ""))
            if m:
                result["appleMusic"] = m.group(1)
                break

        # Deezer
        for entry in links.get("deezer", []):
            m = re.search(r"/track/(\d+)", entry.get("link", ""))
            if m:
                result["deezer"] = m.group(1)
                break

        return result

    # ── Odesli ────────────────────────────────────────────────────────────────

    def _try_odesli(self, track) -> dict[str, str]:
        """Odesli fallback with exponential backoff on 429."""
        params = self._build_odesli_params(track)
        if not params:
            logger.debug(f"[Odesli] No Spotify ID or ISRC for '{track.title}' — skipping.")
            return {}

        for attempt, delay in enumerate([0] + _ODESLI_RETRY_DELAYS):
            if delay:
                logger.debug(
                    f"[Odesli] Rate-limited — retrying in {delay}s "
                    f"(attempt {attempt}/{len(_ODESLI_RETRY_DELAYS)})..."
                )
                time.sleep(delay)
            try:
                resp = self._session.get(_ODESLI_API, params=params, timeout=8)
            except Exception as e:
                logger.debug(f"[Odesli] Request failed: {e}")
                return {}

            if resp.status_code == 429:
                continue
            if not resp.ok:
                logger.debug(f"[Odesli] HTTP {resp.status_code}")
                return {}

            try:
                data = resp.json()
            except Exception as e:
                logger.debug(f"[Odesli] JSON decode failed: {e}")
                return {}

            return self._extract_odesli(data, track.title)

        logger.debug(f"[Odesli] Gave up after all retries for '{track.title}'")
        return {}

    def _build_odesli_params(self, track) -> Optional[dict]:
        params: dict = {}
        if self._api_key:
            params["key"] = self._api_key

        spotify_id = getattr(track, "spotify_id", None)
        if spotify_id:
            params["url"] = f"https://open.spotify.com/track/{spotify_id}"
            params["platform"] = "spotify"
            params["type"] = "song"
            return params

        if getattr(track, "isrc", None):
            params["isrc"] = track.isrc
            params["country"] = "US"
            return params

        return None

    def _extract_odesli(self, data: dict, title: str) -> dict[str, str]:
        links = data.get("linksByPlatform", {})
        entities = data.get("entitiesByUniqueId", {})
        result: dict[str, str] = {}

        for platform, link_info in links.items():
            entity_uid = link_info.get("entityUniqueId", "")
            entity = entities.get(entity_uid, {})
            raw_id = entity.get("id") or (entity_uid.split("::")[-1] if "::" in entity_uid else "")
            if raw_id:
                result[platform] = str(raw_id)
                logger.debug(f"[Odesli] '{title}' → {platform}: {raw_id}")

        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _store(self, cache_key: Optional[str], result: dict) -> None:
        if cache_key and result:
            self._cache[cache_key] = result
            _save_cache(self._cache)
