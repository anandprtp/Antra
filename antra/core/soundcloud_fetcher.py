"""
SoundCloud URL fetcher — extracts track lists from SoundCloud URLs.

Supports:
  - Tracks:    https://soundcloud.com/{user}/{slug}
  - Playlists: https://soundcloud.com/{user}/sets/{slug}

Uses the SoundCloud API v2 (unofficial, same technique as yt-dlp).
No credentials required — a client_id is auto-extracted from the
SoundCloud web player JS on first use, or can be provided via config.

No account, no subscription, no credentials required.
"""

import logging
import re
from typing import Optional

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

_SC_HOME = "https://soundcloud.com"
_SC_API = "https://api-v2.soundcloud.com"
# SoundCloud embeds the client_id in their compiled JS bundles.
# Pattern covers both  client_id:"VALUE"  and  client_id: "VALUE"  forms.
_CLIENT_ID_RE = re.compile(r'client_id\s*:\s*"([a-zA-Z0-9_-]{20,})"')
_CLIENT_ID_SCRIPT_RE = re.compile(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"')

REQUEST_TIMEOUT = 15
MAX_PLAYLIST_PAGES = 20   # safety cap (500 tracks at 25/page)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_soundcloud_url(url: str) -> bool:
    """Return True if the URL looks like a SoundCloud URL."""
    return "soundcloud.com" in url


class SoundCloudFetcher:
    """
    Resolves SoundCloud URLs to lists of TrackMetadata.

    Does not download audio — just collects metadata (title, artist,
    duration) for the Antra waterfall to act on.

    SoundCloud does not expose ISRC in its public API, so the waterfall
    resolver will use fuzzy title/artist matching for source lookup.
    """

    def __init__(self, client_id: Optional[str] = None):
        self._client_id = client_id  # optional — auto-detected if blank
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ── Public entry point ────────────────────────────────────────────────────

    def fetch(self, url: str) -> list[TrackMetadata]:
        """
        Resolve a SoundCloud URL to a list of TrackMetadata objects.
        Raises ValueError for unrecognised or unsupported URLs.
        Raises RuntimeError if the API call fails.
        """
        url = url.strip()
        if not is_soundcloud_url(url):
            raise ValueError(f"[SoundCloud] Not a SoundCloud URL: {url}")

        data = self._resolve(url)
        kind = data.get("kind", "")

        if kind == "track":
            return [self._parse_track(data)]
        elif kind in ("playlist", "system-playlist"):
            return self._fetch_playlist_tracks(data)
        else:
            raise ValueError(
                f"[SoundCloud] Unsupported URL type '{kind}'. "
                "Supported: track, playlist/set URLs."
            )

    # ── API calls ─────────────────────────────────────────────────────────────

    def _resolve(self, url: str) -> dict:
        """Call the SoundCloud resolve endpoint to get entity data."""
        client_id = self._get_client_id()
        try:
            resp = self._session.get(
                f"{_SC_API}/resolve",
                params={"url": url, "client_id": client_id},
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as e:
            raise RuntimeError(f"[SoundCloud] Request failed: {e}") from e

        if resp.status_code == 401:
            # client_id expired — clear cache and retry once
            logger.debug("[SoundCloud] client_id rejected (401), refreshing...")
            self._client_id = None
            client_id = self._get_client_id()
            try:
                resp = self._session.get(
                    f"{_SC_API}/resolve",
                    params={"url": url, "client_id": client_id},
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as e:
                raise RuntimeError(f"[SoundCloud] Retry request failed: {e}") from e

        if resp.status_code == 404:
            raise ValueError(f"[SoundCloud] URL not found: {url}")
        if not resp.ok:
            raise RuntimeError(
                f"[SoundCloud] API error {resp.status_code} for {url}"
            )

        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"[SoundCloud] Invalid JSON response: {e}") from e

    def _fetch_paginated_tracks(self, tracks_uri: str) -> list[dict]:
        """Paginate through a playlist's tracks_uri to get all track objects."""
        client_id = self._get_client_id()
        all_tracks: list[dict] = []
        next_url: Optional[str] = (
            f"{tracks_uri}?client_id={client_id}&limit=50"
            if "?" not in tracks_uri
            else f"{tracks_uri}&client_id={client_id}&limit=50"
        )

        for _ in range(MAX_PLAYLIST_PAGES):
            if not next_url:
                break
            try:
                resp = self._session.get(next_url, timeout=REQUEST_TIMEOUT)
            except Exception as e:
                logger.warning(f"[SoundCloud] Pagination request failed: {e}")
                break
            if not resp.ok:
                logger.warning(f"[SoundCloud] Pagination returned {resp.status_code}")
                break
            try:
                page = resp.json()
            except Exception:
                break
            all_tracks.extend(page.get("collection", []))
            next_url = page.get("next_href")
            if next_url and "client_id" not in next_url:
                next_url += f"&client_id={client_id}"

        return all_tracks

    def _fetch_tracks_by_ids(self, track_ids: list[int | str]) -> list[dict]:
        """Hydrate track stub objects via the /tracks endpoint in chunks."""
        client_id = self._get_client_id()
        results: list[dict] = []
        chunk_size = 50

        for start in range(0, len(track_ids), chunk_size):
            chunk = [str(track_id) for track_id in track_ids[start:start + chunk_size] if track_id]
            if not chunk:
                continue
            try:
                resp = self._session.get(
                    f"{_SC_API}/tracks",
                    params={
                        "ids": ",".join(chunk),
                        "client_id": client_id,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as e:
                logger.warning(f"[SoundCloud] Track hydration request failed: {e}")
                continue

            if not resp.ok:
                logger.warning(f"[SoundCloud] Track hydration returned {resp.status_code}")
                continue

            try:
                data = resp.json()
            except Exception:
                continue

            if isinstance(data, list):
                results.extend(item for item in data if isinstance(item, dict))

        return results

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _fetch_playlist_tracks(self, playlist_data: dict) -> list[TrackMetadata]:
        """
        Extract tracks from a playlist response.
        The initial resolve gives us up to 5 tracks inline; the rest come
        from paginating tracks_uri.
        """
        tracks_uri = playlist_data.get("tracks_uri", "")
        inline_tracks = playlist_data.get("tracks", [])
        total = playlist_data.get("track_count", 0) or 0
        playlist_name = playlist_data.get("title") or ""
        playlist_user = playlist_data.get("user") or {}
        playlist_owner = (
            playlist_user.get("username")
            or playlist_user.get("full_name")
            or ""
        )
        playlist_artwork = (
            playlist_data.get("artwork_url")
            or playlist_data.get("avatar_url")
            or ""
        )
        if playlist_artwork:
            playlist_artwork = playlist_artwork.replace("-large.", "-t500x500.")

        inline_full_tracks = [
            track for track in inline_tracks
            if isinstance(track, dict) and track.get("title")
        ]

        # Only trust inline tracks if they are fully populated. SoundCloud often
        # returns the full item count but most entries are stubs with only an id.
        if inline_full_tracks and total > 0 and len(inline_full_tracks) >= total:
            raw_tracks = inline_full_tracks
        elif tracks_uri:
            # Fetch the full playlist via pagination when inline data is partial.
            raw_tracks = self._fetch_paginated_tracks(tracks_uri)
            if not raw_tracks:
                raw_tracks = inline_full_tracks
        else:
            raw_tracks = inline_full_tracks

        if total > 0 and len(raw_tracks) < total:
            existing_ids = {
                str(track.get("id"))
                for track in raw_tracks
                if isinstance(track, dict) and track.get("id")
            }
            missing_ids = [
                track.get("id")
                for track in inline_tracks
                if isinstance(track, dict)
                and track.get("id")
                and str(track.get("id")) not in existing_ids
            ]
            if missing_ids:
                hydrated = self._fetch_tracks_by_ids(missing_ids)
                if hydrated:
                    hydrated_by_id = {
                        str(track.get("id")): track
                        for track in hydrated
                        if isinstance(track, dict) and track.get("id")
                    }
                    merged_tracks: list[dict] = []
                    seen_ids: set[str] = set()
                    for track in inline_tracks:
                        if not isinstance(track, dict):
                            continue
                        track_id = str(track.get("id") or "")
                        full_track = hydrated_by_id.get(track_id) or track
                        if not track_id or track_id in seen_ids:
                            continue
                        seen_ids.add(track_id)
                        merged_tracks.append(full_track)
                    for track in raw_tracks:
                        if not isinstance(track, dict):
                            continue
                        track_id = str(track.get("id") or "")
                        if track_id and track_id not in seen_ids:
                            seen_ids.add(track_id)
                            merged_tracks.append(track)
                    raw_tracks = merged_tracks or raw_tracks

        result: list[TrackMetadata] = []
        for index, t in enumerate(raw_tracks, start=1):
            if not isinstance(t, dict):
                continue
            # SoundCloud may return stub objects (only id present) for
            # tracks that aren't streamable or have been removed.
            if not t.get("title"):
                continue
            try:
                track = self._parse_track(t)
                track.playlist_name = playlist_name or None
                track.playlist_owner = playlist_owner or None
                track.playlist_artwork_url = playlist_artwork or None
                track.playlist_position = index
                result.append(track)
            except Exception as e:
                logger.debug(f"[SoundCloud] Skipping track due to parse error: {e}")
        return result

    def _parse_track(self, data: dict) -> TrackMetadata:
        """Convert a SoundCloud track API object to TrackMetadata."""
        title = data.get("title") or ""
        # SoundCloud stores the artist as the uploader's display name
        user = data.get("user") or {}
        artist = user.get("username") or user.get("full_name") or ""

        # Duration comes in milliseconds
        duration_ms: Optional[int] = data.get("duration")

        # Artwork: replace "-large" with "-t500x500" for higher resolution
        artwork_url: Optional[str] = data.get("artwork_url")
        if artwork_url:
            artwork_url = artwork_url.replace("-large.", "-t500x500.")

        # Genre
        genre: Optional[str] = data.get("genre") or None

        return TrackMetadata(
            title=title,
            artists=[artist] if artist else [],
            album="",          # SoundCloud tracks don't have albums
            duration_ms=duration_ms,
            isrc=None,         # SoundCloud API does not expose ISRC
            artwork_url=artwork_url,
            genres=[genre] if genre else [],
        )

    # ── client_id management ──────────────────────────────────────────────────

    def _get_client_id(self) -> str:
        """
        Return a valid SoundCloud client_id.
        Order of preference:
          1. Already have one (config override or previously scraped)
          2. Scrape from SoundCloud web player JS
        """
        if self._client_id:
            return self._client_id

        self._client_id = self._scrape_client_id()
        if not self._client_id:
            raise RuntimeError(
                "[SoundCloud] Could not obtain a client_id from SoundCloud. "
                "Set SOUNDCLOUD_CLIENT_ID in your .env to bypass auto-detection."
            )
        logger.debug(f"[SoundCloud] client_id obtained: {self._client_id[:8]}...")
        return self._client_id

    def _scrape_client_id(self) -> Optional[str]:
        """
        Fetch the SoundCloud homepage, find the hashed JS bundle URLs
        embedded in <script> tags, then scan those scripts for client_id.
        """
        logger.debug("[SoundCloud] Scraping client_id from web player...")
        try:
            resp = self._session.get(_SC_HOME, timeout=REQUEST_TIMEOUT)
            if not resp.ok:
                return None
            html = resp.text
        except Exception as e:
            logger.debug(f"[SoundCloud] Homepage fetch failed: {e}")
            return None

        # First try: client_id embedded directly in main HTML
        m = _CLIENT_ID_RE.search(html)
        if m:
            return m.group(1)

        # Second try: scan the linked JS bundles.
        # The client_id lives in the app-config bundle — one of the larger
        # sndcdn asset files. We scan all of them (there are typically ~9)
        # and stop as soon as we find a match.
        script_urls = _CLIENT_ID_SCRIPT_RE.findall(html)
        if not script_urls:
            script_urls = re.findall(
                r'<script[^>]+src="(https://[^"]+\.js)"', html
            )

        for script_url in script_urls:  # scan all asset scripts
            try:
                js_resp = self._session.get(script_url, timeout=10)
                if not js_resp.ok:
                    continue
                m = _CLIENT_ID_RE.search(js_resp.text)
                if m:
                    logger.debug(f"[SoundCloud] Found client_id in {script_url}")
                    return m.group(1)
            except Exception as e:
                logger.debug(f"[SoundCloud] JS fetch failed ({script_url}): {e}")
                continue

        return None
