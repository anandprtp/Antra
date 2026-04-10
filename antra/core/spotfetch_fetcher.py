"""
SpotFetch URL fetcher — retrieves Spotify track/album/playlist metadata
without requiring Spotify credentials.

Uses sp.afkarxyz.qzz.io, a community proxy that wraps the Spotify API
and returns full metadata (title, artists, ISRC, artwork, duration) for
any public Spotify URL.

Supported URLs:
  - Tracks:    https://open.spotify.com/track/{id}
  - Albums:    https://open.spotify.com/album/{id}
  - Playlists: https://open.spotify.com/playlist/{id}
"""

import logging
import re
from typing import Optional

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

_BASE = "https://sp.afkarxyz.qzz.io/api"
_SPOTIFY_ID_RE = re.compile(r"spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)")

REQUEST_TIMEOUT = 15

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class SpotFetchFetcher:
    """
    Resolves Spotify URLs to lists of TrackMetadata via the SpotFetch proxy.

    Does not download audio — just collects metadata (title, artists, ISRC,
    duration, artwork) for the Antra waterfall to act on.

    Intended as a no-credentials fallback when Spotify auth is not configured.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ── Public entry point ────────────────────────────────────────────────────

    def fetch(self, url: str) -> list[TrackMetadata]:
        """
        Resolve a Spotify URL to a list of TrackMetadata objects.
        Raises ValueError for unrecognised URLs.
        Raises RuntimeError if the API call fails.
        """
        url = url.strip()
        url_type, spotify_id = self._detect_type(url)

        if url_type == "track":
            return self._fetch_track(spotify_id)
        elif url_type == "album":
            return self._fetch_album(spotify_id)
        elif url_type == "playlist":
            return self._fetch_playlist(spotify_id)
        else:
            raise ValueError(f"[SpotFetch] Unsupported URL type: {url_type}")

    # ── URL parsing ───────────────────────────────────────────────────────────

    def _detect_type(self, url: str) -> tuple[str, str]:
        """Return (url_type, spotify_id). Raises ValueError on bad URLs."""
        m = _SPOTIFY_ID_RE.search(url)
        if not m:
            raise ValueError(
                f"[SpotFetch] Not a recognised Spotify URL: {url}\n"
                "Supported: open.spotify.com/track/..., /album/..., /playlist/..."
            )
        return m.group(1), m.group(2)

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _fetch_track(self, spotify_id: str) -> list[TrackMetadata]:
        data = self._get(f"/track/{spotify_id}")
        track_data = data.get("track") or data
        return [self._parse_track(track_data)]

    def _fetch_album(self, spotify_id: str) -> list[TrackMetadata]:
        data = self._get(f"/album/{spotify_id}")
        tracks_raw = data.get("tracks") or []
        result = []
        for t in tracks_raw:
            if not isinstance(t, dict):
                continue
            try:
                result.append(self._parse_track(t))
            except Exception as e:
                logger.debug(f"[SpotFetch] Skipping album track: {e}")
        if not result:
            raise RuntimeError(
                f"[SpotFetch] No tracks found in album response for {spotify_id}"
            )
        return result

    def _fetch_playlist(self, spotify_id: str) -> list[TrackMetadata]:
        data = self._get(f"/playlist/{spotify_id}")
        tracks_raw = data.get("tracks") or []
        result = []
        for item in tracks_raw:
            if not isinstance(item, dict):
                continue
            # SpotFetch may nest the track under a "track" key
            t = item.get("track") if "track" in item else item
            if not isinstance(t, dict) or not t.get("name"):
                continue
            try:
                result.append(self._parse_track(t))
            except Exception as e:
                logger.debug(f"[SpotFetch] Skipping playlist track: {e}")
        if not result:
            raise RuntimeError(
                f"[SpotFetch] No tracks found in playlist response for {spotify_id}"
            )
        return result

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """
        Return artist metadata + full album/single/EP list via SpotFetch proxy.
        No Spotify credentials required.
        """
        m = re.search(r"spotify\.com/artist/([A-Za-z0-9]+)", url_or_id)
        artist_id = m.group(1) if m else url_or_id.strip()

        data = self._get(f"/artist/{artist_id}")
        ai = data.get("artist_info", {})
        artist_name = ai.get("name", "Unknown Artist")
        artwork_url = ai.get("images") or None

        albums = []
        for item in data.get("album_list", []):
            album_id = item.get("id", "")
            release_date = item.get("release_date", "")
            year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
            raw_type = item.get("album_type", "ALBUM").lower()
            album_type = raw_type if raw_type in ("album", "single", "compilation") else "album"
            albums.append({
                "id": album_id,
                "url": item.get("external_urls") or f"https://open.spotify.com/album/{album_id}",
                "name": item.get("name", ""),
                "type": album_type,
                "year": year,
                "track_count": item.get("total_tracks", 0),
                "artwork_url": item.get("images") or None,
            })

        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artwork_url": artwork_url,
            "albums": albums,
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, path: str) -> dict:
        url = f"{_BASE}{path}"
        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            raise RuntimeError(f"[SpotFetch] Request failed for {path}: {e}") from e

        if resp.status_code == 404:
            raise ValueError(f"[SpotFetch] Not found: {path}")
        if not resp.ok:
            raise RuntimeError(
                f"[SpotFetch] API error {resp.status_code} for {path}"
            )
        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"[SpotFetch] Invalid JSON response: {e}") from e

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_track(self, data: dict) -> TrackMetadata:
        """Convert a SpotFetch track object to TrackMetadata."""
        title = data.get("name") or data.get("title") or ""

        # artists may be a comma-separated string or a list
        raw_artists = data.get("artists") or ""
        if isinstance(raw_artists, list):
            artists = [a.get("name", a) if isinstance(a, dict) else str(a) for a in raw_artists]
        else:
            artists = [a.strip() for a in str(raw_artists).split(",") if a.strip()]

        album = data.get("album_name") or data.get("album") or ""
        duration_ms: Optional[int] = data.get("duration_ms")
        isrc: Optional[str] = data.get("isrc") or None
        artwork_url: Optional[str] = data.get("artwork_url") or data.get("image") or None
        spotify_id: Optional[str] = data.get("spotify_id") or data.get("id") or None

        return TrackMetadata(
            title=title,
            artists=artists,
            album=album,
            duration_ms=duration_ms,
            isrc=isrc,
            artwork_url=artwork_url,
            spotify_id=spotify_id,
        )
