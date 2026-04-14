"""
SpotFetch URL fetcher — retrieves Spotify track/album/playlist metadata
without requiring Spotify credentials.

Uses a pool of community mirror proxies that wrap the Spotify API and return
full metadata (title, artists, ISRC, artwork, duration) for any public
Spotify URL. Mirrors are tried in order; DNS/connection failures are skipped
immediately so a single down host doesn't block the others.

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

_DEFAULT_BASES = [
    "https://sp.afkarxyz.qzz.io/api",
    "https://sp.vov.li/api",
    "https://sp.rnb.su/api",
    "https://spotify.squid.wtf/api",
]
_SPOTIFY_ID_RE = re.compile(r"spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)")

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
    Tries multiple mirror bases in order; skips mirrors that fail with DNS/
    connection errors so a single down host doesn't block the other mirrors.
    """

    def __init__(self, bases: Optional[list[str]] = None):
        self._bases = [b.rstrip("/") for b in (bases or _DEFAULT_BASES) if b]
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
        elif url_type == "artist":
            return self._fetch_artist(spotify_id)
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

        # The SpotFetch API nests album metadata under "album_info"
        album_info = data.get("album_info") or {}

        # Album-level artwork — "images" key holds a URL string (not a list here)
        album_artwork = (
            album_info.get("images")
            or album_info.get("artwork_url")
            or album_info.get("image")
            or None
        )
        if isinstance(album_artwork, list):
            album_artwork = album_artwork[0].get("url") if album_artwork and isinstance(album_artwork[0], dict) else (album_artwork[0] if album_artwork else None)

        # Album-level artists — "artists" is a comma-separated string like "PARTYNEXTDOOR, Drake"
        raw_album_artists = album_info.get("artists") or data.get("artists") or ""
        if isinstance(raw_album_artists, str):
            album_artists = [a.strip() for a in raw_album_artists.split(",") if a.strip()]
        elif isinstance(raw_album_artists, list):
            album_artists = [
                a.get("name", a) if isinstance(a, dict) else str(a)
                for a in raw_album_artists
            ]
        else:
            album_artists = []

        # Album release date
        album_release_date = album_info.get("release_date") or ""
        album_release_year = int(album_release_date[:4]) if album_release_date and album_release_date[:4].isdigit() else None

        # Tracks are in "track_list" or legacy fallback keys
        tracks_raw = (
            data.get("track_list")
            or data.get("tracks")
            or data.get("items")
            or data.get("trackList")
            or []
        )
        if not tracks_raw:
            logger.debug(f"[SpotFetch] Album response keys for {spotify_id}: {list(data.keys())}")
        result = []
        for t in tracks_raw:
            if not isinstance(t, dict):
                continue
            # Some responses nest the track under a "track" key
            track_data = t.get("track") if "track" in t else t
            if not isinstance(track_data, dict):
                continue
            try:
                track = self._parse_track(track_data)
                # Stamp album-level data that individual tracks don't carry
                if album_artists and not track.album_artists:
                    track.album_artists = album_artists
                if album_artwork and not track.artwork_url:
                    track.artwork_url = album_artwork
                if album_release_year and not track.release_year:
                    track.release_year = album_release_year
                if album_release_date and not track.release_date:
                    track.release_date = album_release_date
                result.append(track)
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

    def _fetch_artist(self, artist_id: str) -> list[TrackMetadata]:
        """Fetch all tracks from an artist's discography via the SpotFetch proxy."""
        info = self.fetch_artist_discography_info(artist_id)
        tracks: list[TrackMetadata] = []
        albums = info.get("albums", [])
        logger.info(f"[SpotFetch] Artist has {len(albums)} releases — fetching tracks...")
        for album in albums:
            album_url = album.get("url", "")
            album_name = album.get("name", "")
            if not album_url:
                continue
            # Extract album ID from URL
            m = _SPOTIFY_ID_RE.search(album_url)
            if not m:
                continue
            album_spotify_id = m.group(2)
            try:
                album_tracks = self._fetch_album(album_spotify_id)
                tracks.extend(album_tracks)
            except Exception as e:
                logger.warning(f"[SpotFetch] Skipping album '{album_name}': {e}")
        if not tracks:
            raise RuntimeError(
                f"[SpotFetch] No tracks found for artist {artist_id}"
            )
        return tracks

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
        last_error: Exception = RuntimeError(f"[SpotFetch] No mirrors available for {path}")
        for base in self._bases:
            url = f"{base}{path}"
            try:
                resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
            except Exception as e:
                # DNS / connection failure — skip this mirror immediately
                logger.debug(f"[SpotFetch] Mirror {base} unreachable: {e}")
                last_error = RuntimeError(f"[SpotFetch] Request failed for {path}: {e}")
                continue

            if resp.status_code == 404:
                raise ValueError(f"[SpotFetch] Not found: {path}")
            if not resp.ok:
                logger.debug(f"[SpotFetch] Mirror {base} returned {resp.status_code} for {path} — trying next")
                last_error = RuntimeError(f"[SpotFetch] API error {resp.status_code} for {path}")
                continue

            try:
                return resp.json()
            except Exception as e:
                last_error = RuntimeError(f"[SpotFetch] Invalid JSON response from {base}: {e}")
                continue

        raise last_error

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

        # album_artist may be a comma-separated string
        raw_album_artist = data.get("album_artist") or ""
        if isinstance(raw_album_artist, str) and raw_album_artist.strip():
            album_artists = [a.strip() for a in raw_album_artist.split(",") if a.strip()]
        else:
            album_artists = []

        album = data.get("album_name") or data.get("album") or ""
        album_id: Optional[str] = data.get("album_id") or None
        duration_ms: Optional[int] = data.get("duration_ms")
        isrc: Optional[str] = data.get("isrc") or None

        # artwork: "images" key is a URL string in this API (not a list)
        raw_images = data.get("images") or data.get("artwork_url") or data.get("image") or None
        if isinstance(raw_images, list):
            artwork_url = raw_images[0].get("url") if raw_images and isinstance(raw_images[0], dict) else (raw_images[0] if raw_images else None)
        else:
            artwork_url = raw_images  # already a string URL

        spotify_id: Optional[str] = data.get("spotify_id") or data.get("id") or None
        track_number: Optional[int] = data.get("track_number")
        disc_number: Optional[int] = data.get("disc_number")
        total_tracks: Optional[int] = data.get("total_tracks")

        release_date: Optional[str] = data.get("release_date") or None
        release_year: Optional[int] = int(release_date[:4]) if release_date and release_date[:4].isdigit() else None

        return TrackMetadata(
            title=title,
            artists=artists,
            album=album,
            album_artists=album_artists,
            album_id=album_id,
            duration_ms=duration_ms,
            isrc=isrc,
            artwork_url=artwork_url,
            spotify_id=spotify_id,
            track_number=track_number,
            disc_number=disc_number,
            total_tracks=total_tracks,
            release_date=release_date,
            release_year=release_year,
        )
