"""
Spotify podcast episode fetcher and downloader.

Uses sp_dc cookie → Bearer token (1-hour TTL, auto-refreshed).
No DRM on podcast audio — plain OGG Vorbis or AAC, downloaded directly from CDN.

Rate limiting: 3–7s random jitter between episode downloads to protect the sp_dc.
Hard cap: 50 episodes/hour (matches Spotify's internal streaming rate limit).

Get sp_dc:  open.spotify.com → DevTools → Application → Cookies → sp_dc

Token strategy:
  Spotify's /get_access_token endpoint is Cloudflare IP-blocked for scripted
  clients (403 "URL Blocked"), even with Chrome TLS impersonation (curl_cffi).
  Instead we use /api/token with a TOTP code (same mechanism Spotify's own web
  player uses). Passing the sp_dc cookie to this endpoint produces a non-
  anonymous authenticated token without triggering Cloudflare.
"""
import logging
import os
import random
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Spotify endpoints
# NOTE: /get_access_token is Cloudflare IP-blocked for scripted clients.
#       We use /api/token + TOTP instead (same as the web player internals).
_TOKEN_URL    = "https://open.spotify.com/api/token"
_CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
_METADATA_URL = "https://spclient.wg.spotify.com/metadata/4/episode"
_RESOLVE_URL  = "https://spclient.wg.spotify.com/storage-resolve/v2/files/audio/interactive/10"
_EPISODE_API  = "https://api.spotify.com/v1/episodes"
_SHOW_API     = "https://api.spotify.com/v1/shows"
_PATHFINDER_API = "https://api-partner.spotify.com/pathfinder/v2/query"
_PLAYBACK_INFO_API = "https://gue1-spclient.spotify.com/track-playback/v1/media/spotify:episode:{episode_id}"
_AUDIO_RESOLVE_API = (
    "https://gue1-spclient.spotify.com/storage-resolve/v2/files/audio/interactive/{format_id}/{file_id}"
)

_CLIENT_VERSION = "1.2.87.27.ga2033a72"
_PF_HASH_EPISODE = "8a62dbdeb7bd79605d7d68b01bcdf83f08bc6c6287ee1665ba012c748a4cf1f3"
_PF_HASH_SHOW = "8e2826c5993383566cc08bf9f5d3301b69513c3f6acb8d706286855e57bf44b2"

# Spotify web-player TOTP credentials (same secret used by spotify.py).
# The /api/token endpoint accepts TOTP codes and the sp_dc cookie together,
# returning isAnonymous=False (authenticated) without any Cloudflare blocking.
_SP_TOTP_SECRET  = (
    "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4D"
    "QOJUGQYTAMRRGA2TCMJSHE3TCMBY"
)
_SP_TOTP_VERSION = 61

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# Spotify IDs are base62; canonical ordering is 0-9 + a-z + A-Z.
# Keep an alternate alphabet fallback because legacy snippets online use
# 0-9 + A-Z + a-z and produce different GIDs.
_BASE62_PRIMARY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE62_ALT     = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Format priority / metadata
_FORMAT_PRIORITY = {
    "OGG_VORBIS_320": 4,
    "OGG_VORBIS_160": 3,
    "MP4_128_DUAL":   2,
    "MP4_128":        2,
    "OGG_VORBIS_96":  1,
}
_FORMAT_CODEC   = {
    "OGG_VORBIS_320": "ogg", "OGG_VORBIS_160": "ogg", "OGG_VORBIS_96": "ogg",
    "MP4_128": "aac", "MP4_128_DUAL": "aac",
}
_FORMAT_BITRATE = {
    "OGG_VORBIS_320": 320, "OGG_VORBIS_160": 160, "OGG_VORBIS_96": 96,
    "MP4_128": 128, "MP4_128_DUAL": 128,
}
_FORMAT_EXT = {
    "OGG_VORBIS_320": ".ogg", "OGG_VORBIS_160": ".ogg", "OGG_VORBIS_96": ".ogg",
    "MP4_128": ".m4a", "MP4_128_DUAL": ".m4a",
}
_DEFAULT_EPISODE_DECRYPTION_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeef"

# Rate-limit config
_MIN_DELAY  = 3.0
_MAX_DELAY  = 7.0
_HOUR_CAP   = 50


class PodcastAlreadyExistsError(Exception):
    """Raised when the episode file already exists on disk (skipped, not failed)."""
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


@dataclass
class PodcastEpisode:
    episode_id:     str
    title:          str
    show_name:      str
    show_id:        str       = ""
    description:    str       = ""
    duration_ms:    int       = 0
    release_date:   Optional[str] = None
    artwork_url:    Optional[str] = None
    episode_number: Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gid_hex(spotify_id: str, alphabet: str = _BASE62_PRIMARY) -> str:
    n = 0
    for c in spotify_id:
        n = n * 62 + alphabet.index(c)
    return n.to_bytes(16, "big").hex()

def _gid_hex_candidates(spotify_id: str) -> list[str]:
    """Generate plausible Spotify GID hex values for a base62 Spotify ID."""
    out: list[str] = []
    for alphabet in (_BASE62_PRIMARY, _BASE62_ALT):
        try:
            gid = _gid_hex(spotify_id, alphabet=alphabet)
        except ValueError:
            continue
        if gid not in out:
            out.append(gid)
    return out


def _extract_id(url: str, kind: str) -> Optional[str]:
    m = re.search(rf'/{kind}/([0-9A-Za-z]{{22}})', url)
    if m:
        return m.group(1)
    if re.fullmatch(r'[0-9A-Za-z]{22}', url.strip()):
        return url.strip()
    return None


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip().strip('.')
    return cleaned[:200] or "Unknown"


def _id_from_uri(uri: str, kind: str) -> str:
    """
    Extract ID from spotify URI form: spotify:{kind}:{id}
    Returns empty string when missing/invalid.
    """
    if not uri:
        return ""
    prefix = f"spotify:{kind}:"
    if uri.startswith(prefix):
        return uri[len(prefix):].strip()
    return ""


def _coerce_duration_ms(value: Any) -> int:
    """Normalize Spotify duration payloads to integer milliseconds."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for k in (
            "totalMilliseconds",
            "total_ms",
            "totalMs",
            "milliseconds",
            "ms",
            "value",
        ):
            inner = value.get(k)
            if isinstance(inner, (int, float)):
                return int(inner)
    return 0


def _coerce_release_date(value: Any) -> Optional[str]:
    """
    Convert Spotify date/timestamp payloads to YYYY-MM-DD when possible.
    Accepts epoch seconds/ms, ISO strings, and date-only strings.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # epoch milliseconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return v
        if re.fullmatch(r"\d{4}", v):
            return f"{v}-01-01"
        if v.isdigit():
            return _coerce_release_date(int(v))
        if "T" in v and len(v) >= 10:
            maybe = v[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", maybe):
                return maybe
    return None


def _first_image_url(*candidates: Any) -> Optional[str]:
    """Best-effort extraction of artwork URL from various Spotify payload shapes."""
    for value in candidates:
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        if isinstance(value, dict):
            # Single-image dict
            if isinstance(value.get("url"), str) and value["url"].startswith(("http://", "https://")):
                return value["url"]
            # Wrapped image dicts
            for key in ("image", "images", "covers", "sources"):
                inner = value.get(key)
                found = _first_image_url(inner)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = _first_image_url(item)
                if found:
                    return found
    return None


def is_podcast_url(url: str) -> bool:
    """True if url points to a Spotify episode or show page."""
    return bool(
        re.search(r'open\.spotify\.com/(episode|show)/', url)
    )


# ── Client ────────────────────────────────────────────────────────────────────

class SpotifyPodcastClient:
    """
    Fetches Spotify podcast metadata and CDN stream URLs via sp_dc cookie auth.
    Thread-safe token cache; single-instance per sp_dc is the expected usage.
    """

    def __init__(self, sp_dc: str):
        if not sp_dc:
            raise ValueError(
                "Spotify sp_dc cookie is required for podcast downloads. "
                "Open Spotify in a browser, go to DevTools → Application → "
                "Cookies → open.spotify.com → sp_dc, and paste the value in "
                "Settings → Spotify Podcasts."
            )
        self._sp_dc = sp_dc
        self._token: Optional[str] = None
        self._client_token: Optional[str] = None
        self._token_expires: float = 0.0
        self._lock = threading.Lock()

        # Rate-limit state (not thread-safe on purpose — single-threaded download loop)
        self._last_ts: float = 0.0
        self._hour_count: int = 0
        self._hour_start: float = time.time()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_expires - 120:
                return self._token

            token, expires, client_id = self._fetch_token_via_totp()
            client_token = self._fetch_client_token(client_id) if client_id else None
            self._token, self._token_expires = token, expires
            self._client_token = client_token
            logger.info("[Podcast] Bearer token refreshed, expires %s",
                        time.strftime("%H:%M:%S", time.localtime(expires)))
            if client_token:
                logger.debug("[Podcast] client-token refreshed")
            return token

    def _fetch_token_via_totp(self) -> tuple[str, float, Optional[str]]:
        """
        Exchange sp_dc cookie for an authenticated Bearer token using the
        Spotify web player's TOTP mechanism.

        Uses /api/token (not /get_access_token) — the latter is Cloudflare
        IP-blocked for scripted clients even with Chrome TLS impersonation.
        Passing sp_dc + a fresh TOTP code to /api/token returns
        isAnonymous=False without triggering any bot detection.
        """
        try:
            import pyotp
        except ImportError:
            raise RuntimeError(
                "pyotp is required for podcast downloads. Run: pip install pyotp"
            )

        totp = pyotp.TOTP(_SP_TOTP_SECRET)
        code = totp.now()

        r = requests.get(
            _TOKEN_URL,
            params={
                "reason":      "init",
                "productType": "web-player",
                "totp":        code,
                "totpVer":     str(_SP_TOTP_VERSION),
                "totpServer":  code,
            },
            headers={
                "User-Agent":  (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                "Accept":          "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin":          "https://open.spotify.com",
                "Referer":         "https://open.spotify.com/",
            },
            cookies={"sp_dc": self._sp_dc},
            timeout=15,
        )

        if r.status_code == 401:
            raise RuntimeError(
                "Spotify sp_dc cookie is invalid or expired. "
                "Refresh it from your browser (DevTools → Application → "
                "Cookies → open.spotify.com → sp_dc) and update it in Settings."
            )
        r.raise_for_status()
        data = r.json()
        token = data.get("accessToken")
        if not token or data.get("isAnonymous", True):
            raise RuntimeError(
                "sp_dc cookie produced an anonymous token — it may be expired or "
                "not associated with a Spotify account. "
                "Refresh it from your browser."
            )
        expires = (data.get("accessTokenExpirationTimestampMs") or 0) / 1000 or time.time() + 3600
        return token, expires, data.get("clientId")

    def _fetch_client_token(self, client_id: str) -> Optional[str]:
        if not client_id:
            return None
        try:
            r = requests.post(
                _CLIENT_TOKEN_URL,
                json={
                    "client_data": {
                        "client_version": _CLIENT_VERSION,
                        "client_id": client_id,
                        "js_sdk_data": {},
                    }
                },
                headers={
                    "User-Agent": _UA,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://open.spotify.com",
                    "Referer": "https://open.spotify.com/",
                },
                timeout=15,
            )
            if not r.ok:
                logger.debug("[Podcast] client-token request failed: %s", r.status_code)
                return None
            data = r.json()
            return ((data.get("granted_token") or {}).get("token")) or None
        except Exception as e:
            logger.debug("[Podcast] client-token request error: %s", e)
            return None

    def _api_headers(self) -> dict:
        headers = {
            "User-Agent":             _UA,
            "Authorization":          f"Bearer {self._get_token()}",
            "Accept":                 "application/json",
            "Accept-Language":        "en-US,en;q=0.9",
            "app-platform":           "WebPlayer",
            "spotify-app-version":    "1.2.46.25.g7f189073",
            "Origin":                 "https://open.spotify.com",
            "Referer":                "https://open.spotify.com/",
        }
        if self._client_token:
            headers["client-token"] = self._client_token
        return headers

    def _request(self, method: str, url: str, max_retries: int = 6, **kwargs) -> requests.Response:
        """
        Request with automatic 401 token refresh and 429 backoff.
        Used for both GET and POST Spotify endpoints.
        """
        last: Optional[requests.Response] = None
        for attempt in range(max_retries):
            r = requests.request(method, url, headers=self._api_headers(), timeout=15, **kwargs)
            last = r

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after) + 0.5
                else:
                    # Exponential fallback similar to generic retry transports.
                    wait = min(2 ** attempt, 30)
                wait = min(wait, 30.0)
                logger.info(
                    "[Podcast] Rate-limited (429) — waiting %.1fs (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue

            if r.status_code == 401 and attempt < max_retries - 1:
                logger.info("[Podcast] Token expired (401) — refreshing")
                self._get_token(force=True)
                continue

            return r
        return last if last is not None else requests.Response()

    def _get(self, url: str, max_retries: int = 5, **kwargs) -> requests.Response:
        """GET with automatic 401 token refresh and 429 Retry-After back-off."""
        r = self._request("GET", url, max_retries=max_retries, **kwargs)
        r.raise_for_status()
        return r

    def _post(self, url: str, max_retries: int = 6, **kwargs) -> requests.Response:
        r = self._request("POST", url, max_retries=max_retries, **kwargs)
        r.raise_for_status()
        return r

    def _pathfinder(self, operation_name: str, sha256_hash: str, variables: dict) -> dict:
        payload = {
            "variables": variables,
            "operationName": operation_name,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": sha256_hash,
                }
            },
        }
        r = self._post(_PATHFINDER_API, json=payload)
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"Pathfinder error: {data['errors']}")
        return data


    # ── Rate limit ────────────────────────────────────────────────────────────

    def rate_limit(self):
        """
        Enforce inter-download delay and hourly cap.
        Call this once per episode *before* making the stream URL API call.
        """
        now = time.time()
        if now - self._hour_start >= 3600:
            self._hour_count = 0
            self._hour_start = now
        if self._hour_count >= _HOUR_CAP:
            wait = 3600 - (time.time() - self._hour_start)
            logger.warning(
                "[Podcast] Hourly cap (%d/hr) reached — waiting %.0fs", _HOUR_CAP, wait
            )
            time.sleep(max(0, wait))
            self._hour_count = 0
            self._hour_start = time.time()

        elapsed = time.time() - self._last_ts
        delay   = random.uniform(_MIN_DELAY, _MAX_DELAY)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_ts = time.time()
        self._hour_count += 1

    # ── Metadata ──────────────────────────────────────────────────────────────

    def _fetch_spclient_episode_payload(self, episode_id: str) -> dict:
        """
        Fetch episode payload from Spotify spclient metadata endpoint.
        Tries multiple ID encodings and market parameter variants for resilience.
        """
        attempts: list[tuple[str, Optional[dict]]] = []
        gids = _gid_hex_candidates(episode_id)
        for gid in gids:
            base = f"{_METADATA_URL}/{gid}"
            attempts.extend([
                (base, {"market": "from_token"}),
                (base, {"market": "US"}),
                (base, None),
            ])
        # Some environments accept raw base62 ID directly.
        base62_path = f"{_METADATA_URL}/{episode_id}"
        attempts.extend([
            (base62_path, {"market": "from_token"}),
            (base62_path, {"market": "US"}),
            (base62_path, None),
        ])

        last_err: Optional[Exception] = None
        for url, params in attempts:
            try:
                r = self._get(url, params=params)
                return r.json()
            except requests.HTTPError as e:
                # Keep trying alternate shapes on 404/400; for other statuses,
                # bubble up immediately unless it's a transient rate-limit.
                status = e.response.status_code if e.response is not None else None
                if status in (400, 404):
                    last_err = e
                    logger.debug(
                        "[Podcast] spclient metadata miss (%s) for %s params=%s",
                        status, url, params,
                    )
                    continue
                raise
            except Exception as e:
                last_err = e
                continue

        if last_err:
            raise last_err
        raise RuntimeError("spclient metadata fetch failed with no attempts")

    def _fetch_episode_via_spclient(self, episode_id: str) -> PodcastEpisode:
        """
        Fetch episode metadata from Spotify's spclient endpoint.
        This avoids the stricter throttling on the public Web API /v1/episodes path.
        """
        data = self._fetch_spclient_episode_payload(episode_id)

        show = data.get("show") or data.get("podcast") or {}
        title = (
            data.get("name")
            or data.get("title")
            or f"Episode {episode_id}"
        )
        show_name = (
            show.get("name")
            or data.get("show_name")
            or "Spotify Podcast"
        )
        show_id = (
            show.get("id")
            or _id_from_uri(show.get("uri", ""), "show")
            or ""
        )
        description = (
            data.get("description")
            or data.get("short_description")
            or ""
        )
        duration_ms = (
            _coerce_duration_ms(data.get("duration"))
            or _coerce_duration_ms(data.get("duration_ms"))
        )
        release_date = _coerce_release_date(
            data.get("release_date")
            or data.get("publish_date")
            or data.get("publish_time")
        )
        artwork_url = _first_image_url(
            data.get("images"),
            data.get("image"),
            data.get("covers"),
            show.get("images"),
            show.get("image"),
            show.get("covers"),
        )
        episode_number = data.get("episode_number") or data.get("number")
        if isinstance(episode_number, str) and episode_number.isdigit():
            episode_number = int(episode_number)

        return PodcastEpisode(
            episode_id=episode_id,
            title=title,
            show_name=show_name,
            show_id=show_id,
            description=description,
            duration_ms=duration_ms,
            release_date=release_date,
            artwork_url=artwork_url,
            episode_number=episode_number if isinstance(episode_number, int) else None,
        )

    def fetch_episode(self, url_or_id: str) -> PodcastEpisode:
        episode_id = _extract_id(url_or_id, "episode")
        if not episode_id:
            raise ValueError(f"Cannot extract episode ID from: {url_or_id}")

        # Primary path: partner GraphQL (same family used by Votify).
        try:
            data = self._pathfinder(
                operation_name="getEpisodeOrChapter",
                sha256_hash=_PF_HASH_EPISODE,
                variables={"uri": f"spotify:episode:{episode_id}"},
            )
            ep = ((data.get("data") or {}).get("episodeUnionV2")) or {}
            if ep and ep.get("__typename") == "Episode":
                show = ((ep.get("podcastV2") or {}).get("data")) or {}
                images = ((ep.get("coverArt") or {}).get("sources")) or []
                release = ep.get("releaseDate") or {}
                release_iso = release.get("isoString") if isinstance(release, dict) else None
                return PodcastEpisode(
                    episode_id=episode_id,
                    title=ep.get("name", f"Episode {episode_id}"),
                    show_name=show.get("name", "Spotify Podcast"),
                    show_id=_id_from_uri(show.get("uri", ""), "show"),
                    description=ep.get("description", ""),
                    duration_ms=_coerce_duration_ms(ep.get("duration") or ep.get("duration_ms")),
                    release_date=_coerce_release_date(release_iso),
                    artwork_url=_first_image_url(images),
                    episode_number=None,
                )
        except Exception as e:
            logger.warning(
                "[Podcast] Pathfinder metadata failed for episode %s: %s. "
                "Falling back to spclient.",
                episode_id,
                e,
            )

        # Primary path: spclient metadata endpoint.
        # This endpoint is generally more reliable than /v1/episodes for
        # cookie-authenticated podcast scraping and avoids frequent 429s.
        try:
            return self._fetch_episode_via_spclient(episode_id)
        except Exception as e:
            logger.warning(
                "[Podcast] spclient metadata failed for episode %s: %s. "
                "Falling back to Web API.",
                episode_id,
                e,
            )

        # Fallback: public Web API endpoint.
        try:
            r = self._get(f"{_EPISODE_API}/{episode_id}", params={"market": "US"})
            data = r.json()
            show = data.get("show") or {}
            imgs = data.get("images") or show.get("images") or []
            return PodcastEpisode(
                episode_id=episode_id,
                title=data.get("name", f"Episode {episode_id}"),
                show_name=show.get("name", "Spotify Podcast"),
                show_id=show.get("id", ""),
                description=data.get("description", ""),
                duration_ms=data.get("duration_ms", 0),
                release_date=data.get("release_date"),
                artwork_url=imgs[0]["url"] if imgs else None,
                episode_number=data.get("episode_number"),
            )
        except Exception as e:
            logger.warning(
                "[Podcast] Web API episode metadata also failed for %s: %s. "
                "Continuing with minimal metadata.",
                episode_id,
                e,
            )
            return PodcastEpisode(
                episode_id=episode_id,
                title=f"Episode {episode_id}",
                show_name="Spotify Podcast",
            )

    def fetch_show(self, url_or_id: str) -> tuple[str, list[PodcastEpisode]]:
        """Return (show_name, episodes) for all episodes of a podcast show."""
        show_id = _extract_id(url_or_id, "show")
        if not show_id:
            raise ValueError(f"Cannot extract show ID from: {url_or_id}")

        # Primary path: partner GraphQL (less 429-prone than /v1/shows endpoints).
        try:
            episodes: list[PodcastEpisode] = []
            offset = 0
            show_name = "Unknown Show"
            total = None
            while total is None or offset < total:
                data = self._pathfinder(
                    operation_name="queryPodcastEpisodes",
                    sha256_hash=_PF_HASH_SHOW,
                    variables={
                        "uri": f"spotify:show:{show_id}",
                        "offset": offset,
                        "limit": 50,
                    },
                )
                pod = ((data.get("data") or {}).get("podcastUnionV2")) or {}
                if pod.get("__typename") != "Podcast":
                    break
                show_name = pod.get("name", show_name)
                epv2 = pod.get("episodesV2") or {}
                items = epv2.get("items") or []
                total = epv2.get("totalCount") or len(items)
                if not items:
                    break
                for item in items:
                    ent = item.get("entity") or {}
                    if ent.get("__typename") != "Episode":
                        continue
                    eid = _id_from_uri(ent.get("uri", ""), "episode")
                    if not eid:
                        continue
                    show = ((ent.get("podcastV2") or {}).get("data")) or {}
                    imgs = ((ent.get("coverArt") or {}).get("sources")) or []
                    release = ent.get("releaseDate") or {}
                    episodes.append(PodcastEpisode(
                        episode_id=eid,
                        title=ent.get("name", f"Episode {eid}"),
                        show_name=show.get("name", show_name),
                        show_id=show_id,
                        description=ent.get("description", ""),
                        duration_ms=_coerce_duration_ms(ent.get("duration") or ent.get("duration_ms")),
                        release_date=_coerce_release_date(release.get("isoString") if isinstance(release, dict) else None),
                        artwork_url=_first_image_url(imgs),
                        episode_number=None,
                    ))
                offset += len(items)
            if episodes:
                return show_name, episodes
        except Exception as e:
            logger.warning("[Podcast] Pathfinder show metadata failed for %s: %s. Falling back to Web API.", show_id, e)

        # Show-level metadata
        show_data   = self._get(f"{_SHOW_API}/{show_id}", params={"market": "US"}).json()
        show_name   = show_data.get("name", "Unknown Show")
        show_imgs   = show_data.get("images") or []
        show_art    = show_imgs[0]["url"] if show_imgs else None

        # Paginate episodes
        episodes: list[PodcastEpisode] = []
        offset = 0
        while True:
            data  = self._get(
                f"{_SHOW_API}/{show_id}/episodes",
                params={"market": "US", "limit": 50, "offset": offset},
            ).json()
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                if not item:
                    continue
                imgs = item.get("images") or []
                episodes.append(PodcastEpisode(
                    episode_id    = item.get("id", ""),
                    title         = item.get("name", "Unknown Episode"),
                    show_name     = show_name,
                    show_id       = show_id,
                    description   = item.get("description", ""),
                    duration_ms   = item.get("duration_ms", 0),
                    release_date  = item.get("release_date"),
                    artwork_url   = imgs[0]["url"] if imgs else show_art,
                    episode_number= item.get("episode_number"),
                ))
            offset += len(items)
            if offset >= (data.get("total") or 0) or len(items) < 50:
                break

        return show_name, episodes

    # ── Stream URL ────────────────────────────────────────────────────────────

    def get_stream_url(self, episode_id: str) -> dict:
        """
        Resolve episode ID to a CDN stream URL.
        Returns: {streamUrl, codec, format, bitrate, ext, quality_label}
        """
        # Primary path (Votify-like): playback info -> file_id -> storage-resolve.
        # This avoids brittle metadata/4 lookups and works even when episode
        # metadata endpoints only expose preview URLs.
        try:
            playback = self._get(
                _PLAYBACK_INFO_API.format(episode_id=episode_id),
                params={"manifestFileFormat": ["file_ids_mp4", "manifest_ids_video"]},
            ).json()
            media = playback.get("media") or {}
            item = ((media.get(f"spotify:episode:{episode_id}") or {}).get("item")) or {}
            manifest = item.get("manifest") or {}
            files = manifest.get("file_ids_mp4") or []
            files = [f for f in files if f.get("file_id")]
            if files:
                # Prefer highest available bitrate.
                best = max(
                    files,
                    key=lambda f: int(
                        f.get("average_bitrate")
                        or f.get("bitrate")
                        or 0
                    ),
                )
                file_id = best.get("file_id")
                format_id = str(best.get("format") or "10")
                resolve = self._get(
                    _AUDIO_RESOLVE_API.format(format_id=format_id, file_id=file_id),
                    params={
                        "version": "10000000",
                        "product": "9",
                        "platform": "39",
                        "alt": "json",
                    },
                ).json()
                cdn_urls = resolve.get("cdnurl") or []
                if cdn_urls:
                    bitrate_raw = best.get("average_bitrate") or best.get("bitrate") or 128000
                    bitrate = int(int(bitrate_raw) / 1000)
                    return {
                        "streamUrl": cdn_urls[0],
                        "codec": "aac",
                        "format": f"MP4_{bitrate}",
                        "bitrate": bitrate,
                        "ext": ".m4a",
                        "quality_label": f"AAC {bitrate}kbps",
                    }
        except Exception as e:
            logger.warning(
                "[Podcast] Playback-info stream resolve failed for %s: %s. "
                "Falling back to metadata/4 path.",
                episode_id,
                e,
            )

        # Legacy fallback path: metadata/4 -> audio file -> storage-resolve.
        token = self._get_token()
        spc_headers = {
            "User-Agent":          _UA,
            "Authorization":       f"Bearer {token}",
            "Accept":              "application/json",
            "app-platform":        "WebPlayer",
            "spotify-app-version": "1.2.46.25.g7f189073",
        }

        def _spc_get(url: str, max_retries: int = 5, **kwargs) -> requests.Response:
            for attempt in range(max_retries):
                r = requests.get(url, headers=spc_headers, timeout=15, **kwargs)

                if r.status_code == 401 and attempt < max_retries - 1:
                    token2 = self._get_token(force=True)
                    spc_headers["Authorization"] = f"Bearer {token2}"
                    continue

                if r.status_code == 429 and attempt < max_retries - 1:
                    retry_after = int(r.headers.get("Retry-After", 2))
                    wait = min(retry_after + 0.5, 30)
                    logger.info(
                        "[Podcast] spclient rate-limited (429) — waiting %.1fs (attempt %d/%d)",
                        wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue

                return r
            return r

        # Metadata payload from spclient with robust ID conversion fallbacks.
        last_meta_err: Optional[str] = None
        meta: Optional[dict] = None
        meta_attempts: list[tuple[str, Optional[dict]]] = []
        for gid in _gid_hex_candidates(episode_id):
            p = f"{_METADATA_URL}/{gid}"
            meta_attempts.extend([
                (p, {"market": "from_token"}),
                (p, {"market": "US"}),
                (p, None),
            ])
        p2 = f"{_METADATA_URL}/{episode_id}"
        meta_attempts.extend([
            (p2, {"market": "from_token"}),
            (p2, {"market": "US"}),
            (p2, None),
        ])
        for url, params in meta_attempts:
            r_meta = _spc_get(url, params=params)
            if r_meta.status_code in (400, 404):
                continue
            if r_meta.status_code == 404:
                continue
            try:
                r_meta.raise_for_status()
                meta = r_meta.json()
                break
            except Exception as e:
                last_meta_err = str(e)
                continue
        if not meta:
            msg = last_meta_err or "no matching metadata endpoint/path for episode"
            raise RuntimeError(
                "Episode metadata unavailable on Spotify spclient "
                f"({msg}). It may be region-locked, removed, or video-only."
            )

        audio_files: list[dict] = (
            (meta.get("audio") or {}).get("file")
            or meta.get("file")
            or []
        )
        if not audio_files:
            raise RuntimeError(
                "No audio files in episode metadata — the episode may be video-only "
                "or unavailable in your region."
            )

        best    = max(audio_files, key=lambda f: _FORMAT_PRIORITY.get(f.get("format", ""), 0))
        fmt     = best.get("format", "")
        file_id = best.get("file_id", "")
        if not file_id:
            raise RuntimeError("No file_id in selected audio entry")

        logger.info("[Podcast] Format=%s file_id=%s...", fmt, file_id[:12])

        cdn_urls = _spc_get(f"{_RESOLVE_URL}/{file_id}", params={"product": "9"}).json().get("cdnurl") or []
        if not cdn_urls:
            raise RuntimeError("storage-resolve returned no CDN URLs")

        bitrate = _FORMAT_BITRATE.get(fmt, 160)
        codec   = _FORMAT_CODEC.get(fmt, "ogg")
        return {
            "streamUrl":     cdn_urls[0],
            "codec":         codec,
            "format":        fmt,
            "bitrate":       bitrate,
            "ext":           _FORMAT_EXT.get(fmt, ".ogg"),
            "quality_label": f"{codec.upper()} {bitrate}kbps",
        }


# ── Downloader ────────────────────────────────────────────────────────────────

class PodcastDownloader:
    """Downloads and tags Spotify podcast episodes to disk."""

    def __init__(self, client: SpotifyPodcastClient, output_dir: str):
        self._client     = client
        self._output_dir = output_dir

    def download_episode(self, episode: PodcastEpisode) -> tuple[str, str]:
        """
        Download one episode.  Applies rate-limit delay, resolves stream URL,
        streams file to disk, writes tags.

        Returns (file_path, quality_label).
        Raises PodcastAlreadyExistsError if file already exists (caller should skip).
        """
        self._client.rate_limit()
        stream = self._client.get_stream_url(episode.episode_id)

        show_dir = Path(self._output_dir) / "Podcasts" / _safe_filename(episode.show_name)
        show_dir.mkdir(parents=True, exist_ok=True)

        date_prefix = (episode.release_date[:10] + " - ") if episode.release_date else ""
        filename    = _safe_filename(f"{date_prefix}{episode.title}") + stream["ext"]
        out_path    = show_dir / filename

        if out_path.exists():
            logger.info("[Podcast] Already exists: %s", out_path.name)
            raise PodcastAlreadyExistsError(str(out_path))

        logger.info(
            "[Podcast] Downloading: %s — %s (%s)",
            episode.show_name, episode.title, stream["quality_label"],
        )
        with requests.get(stream["streamUrl"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)

        # Some Spotify podcast MP4 streams are encrypted (CENC) and need a
        # static episode key to be playable. Decrypt in-place when detected.
        self._decrypt_episode_if_needed(str(out_path), stream["codec"])

        logger.info("[Podcast] Saved: %s", out_path)
        self._tag_episode(str(out_path), episode, stream["codec"])
        return str(out_path), stream["quality_label"]

    def _decrypt_episode_if_needed(self, path: str, codec: str) -> None:
        if codec != "aac" or not path.lower().endswith(".m4a"):
            return
        if not self._is_encrypted_mp4(path):
            return

        ffmpeg = "ffmpeg"
        tmp_out = path + ".decrypted.m4a"
        try:
            cmd = [
                ffmpeg,
                "-loglevel", "error",
                "-hide_banner",
                "-y",
                "-decryption_key", _DEFAULT_EPISODE_DECRYPTION_KEY_HEX,
                "-i", path,
                "-c", "copy",
                tmp_out,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0 or not os.path.exists(tmp_out):
                logger.warning(
                    "[Podcast] Decryption failed for %s (rc=%s): %s",
                    os.path.basename(path),
                    proc.returncode,
                    (proc.stderr or proc.stdout or "").strip()[:400],
                )
                return
            os.replace(tmp_out, path)
            logger.info("[Podcast] Decrypted episode audio: %s", os.path.basename(path))
        except FileNotFoundError:
            logger.warning(
                "[Podcast] ffmpeg not found; encrypted podcast may be unplayable. "
                "Install ffmpeg and retry."
            )
        except Exception as e:
            logger.warning("[Podcast] Episode decryption failed for %s: %s", path, e)
        finally:
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass

    @staticmethod
    def _is_encrypted_mp4(path: str) -> bool:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return False
            return "Encryption initialization data" in (proc.stdout or "")
        except Exception:
            return False

    def _tag_episode(self, path: str, ep: PodcastEpisode, codec: str):
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            return

        artwork_data: Optional[bytes] = None
        if ep.artwork_url:
            try:
                ar = requests.get(ep.artwork_url, timeout=10)
                if ar.ok:
                    artwork_data = ar.content
            except Exception:
                pass

        try:
            audio = MutagenFile(path, easy=False)
            if audio is None:
                return

            tname = type(audio).__name__

            if tname in ("OggVorbis", "OggOpus"):
                audio["title"]   = ep.title
                audio["artist"]  = ep.show_name
                audio["album"]   = ep.show_name
                audio["comment"] = ep.description[:500] if ep.description else ""
                if ep.release_date:
                    audio["date"] = ep.release_date[:4]
                if ep.episode_number:
                    audio["tracknumber"] = str(ep.episode_number)
                if artwork_data:
                    import base64
                    from mutagen.flac import Picture
                    pic = Picture()
                    pic.type, pic.mime, pic.data = 3, "image/jpeg", artwork_data
                    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]

            elif tname == "MP4":
                from mutagen.mp4 import MP4Cover
                audio["\xa9nam"] = [ep.title]
                audio["\xa9ART"] = [ep.show_name]
                audio["\xa9alb"] = [ep.show_name]
                if ep.description:
                    audio["desc"] = [ep.description[:500]]
                if ep.release_date:
                    audio["\xa9day"] = [ep.release_date[:4]]
                if ep.episode_number:
                    audio["trkn"] = [(ep.episode_number, 0)]
                if artwork_data:
                    audio["covr"] = [MP4Cover(artwork_data, imageformat=MP4Cover.FORMAT_JPEG)]

            elif tname == "MP3":
                from mutagen.id3 import TIT2, TPE1, TALB, COMM, TDRC, TRCK, APIC
                if audio.tags is None:
                    audio.add_tags()
                audio["TIT2"] = TIT2(encoding=3, text=ep.title)
                audio["TPE1"] = TPE1(encoding=3, text=[ep.show_name])
                audio["TALB"] = TALB(encoding=3, text=ep.show_name)
                if ep.description:
                    audio["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=ep.description[:500])
                if ep.release_date:
                    audio["TDRC"] = TDRC(encoding=3, text=ep.release_date[:10])
                if ep.episode_number:
                    audio["TRCK"] = TRCK(encoding=3, text=str(ep.episode_number))
                if artwork_data:
                    audio["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=artwork_data)

            audio.save()
        except Exception as e:
            logger.warning("[Podcast] Tagging failed for %s: %s", path, e)
