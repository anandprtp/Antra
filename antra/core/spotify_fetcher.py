import logging
import requests
import re
import time
from typing import Optional
from antra.core.models import TrackMetadata


def _normalize_spotify_url(url_or_id: str) -> str:
    return re.sub(r"(spotify\.com/)(?:intl-[^/]+/)", r"\1", url_or_id or "").strip()

logger = logging.getLogger(__name__)

class SpotifyFetcher:
    """
    Fetches Spotify playlist and library data.
    
    Supports two auth methods (checked in order):
    1. SpotifyWebPlayerAuth (sp_dc cookie) — preferred, no restrictions
    2. SpotifyAuthManager (PKCE OAuth) — fallback, requires developer app
    
    At least one must be configured.
    """

    def __init__(self, auth_manager=None, web_player_auth=None):
        """
        auth_manager: existing SpotifyAuthManager (PKCE), optional
        web_player_auth: SpotifyWebPlayerAuth (sp_dc), optional
        At least one must be provided.
        """
        self._auth = auth_manager
        self._web_auth = web_player_auth

        if not self._auth and not self._web_auth:
            raise ValueError(
                "[Spotify] SpotifyFetcher requires at least one auth method."
            )

    def get_auth_method(self) -> str:
        """Returns which auth method is currently active."""
        if self._web_auth and self._web_auth.is_valid():
            # If we have a token that hasn't expired yet, it's an override 
            # (either manually set or successfully refreshed)
            if getattr(self._web_auth, "_access_token", None):
                 # If sp_dc is missing, it's definitely a manual override
                 if not getattr(self._web_auth, "_sp_dc", None):
                     return "web_player (token override)"
                 # Even if sp_dc is present, if the token is valid, we report override first
                 return "web_player (token override)"
            return "web_player (sp_dc)"
        if self._auth:
            return "pkce"
        return "none"

    def _get_headers(self) -> dict:
        """
        Get auth headers. Prefers web player (no restrictions).
        Falls back to PKCE if web player unavailable.
        """
        if self._web_auth:
            try:
                return self._web_auth.get_headers()
            except Exception as e:
                logger.debug(
                    f"[Spotify] Web player auth unavailable, falling back to PKCE: {e}"
                )

        if self._auth:
            sp = self._auth.authenticate()
            token_info = sp.auth_manager.get_access_token()
            token = token_info["access_token"] if isinstance(token_info, dict) else token_info
            return {"Authorization": f"Bearer {token}"}

        raise RuntimeError(
            "[Spotify] No working authentication method available. "
            "Set SPOTIFY_SP_DC in your .env or run: antra spotify login"
        )

    def _get_with_retry(
        self, 
        url: str, 
        params: dict = None, 
        tries: int = 4
    ) -> requests.Response:
        """
        Execute a GET request with automatic retry for 401s and 429s.
        Handle token expiry and Spotify rate limits via Retry-After.
        """
        for i in range(tries):
            headers = self._get_headers()
            try:
                r = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=15,
                )
            except Exception as e:
                if i < tries - 1:
                    logger.warning(
                        f"[Spotify] Network error for {url}: {e}. Retrying {i+2}/{tries}..."
                    )
                    time.sleep(1 + i)
                    continue
                raise

            # 401: Unauthorized / Expired
            if r.status_code == 401 and i < tries - 1:
                logger.debug(f"[Spotify] 401 Unauthorized for {url}, refreshing token")
                if self._web_auth:
                    self._web_auth.force_refresh()
                # Continue to next try with fresh headers
                continue

            # 429: Too Many Requests
            if r.status_code == 429:
                if i < tries - 1:
                    # Spotify includes 'Retry-After' in seconds
                    retry_after = int(r.headers.get("Retry-After", 5 + (i * 5)))
                    
                    # Capped wait time (don't hang for hours)
                    MAX_WAIT = 60
                    if retry_after > MAX_WAIT:
                        logger.error(
                            f"[Spotify] 429 Rate Limited. Spotify requested {retry_after}s wait, "
                            f"which exceeds the maximum allowed wait of {MAX_WAIT}s. "
                            "Try again later or update your authentication (sp_dc/token)."
                        )
                        return r

                    wait_time = retry_after + 1
                    logger.warning(
                        f"[Spotify] 429 Too Many Requests. "
                        f"Waiting {wait_time}s before retry {i+2}/{tries}..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    # Last try, don't sleep, just return so raise_for_status() handles it
                    return r

            # Return success or terminal non-429/401 failure
            return r

        return r # Should not reach

    def _paginate(self, url: str, params: dict = None) -> list[dict]:
        """
        Fetch all pages from a paginated Spotify endpoint.
        Handles token expiry and rate limiting naturally.
        Returns flat list of all items across all pages.
        """
        items = []
        current_url = url

        while current_url:
            # For subsequent pages, params are already in the URL
            actual_params = params if current_url == url else None
            
            r = self._get_with_retry(current_url, params=actual_params)
            r.raise_for_status()

            data = r.json()
            items.extend(data.get("items", []))
            current_url = data.get("next")

        return items

    def fetch_playlist_tracks(self, playlist_url: str) -> list[TrackMetadata]:
        """
        Fetch all tracks from a playlist URL or ID.
        Works for public and private playlists.
        Handles pagination automatically.
        """
        playlist_id = self._extract_playlist_id(playlist_url)
        logger.debug(f"[Spotify] Fetching tracks for playlist: {playlist_id}")

        raw_items = self._paginate(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            params={
                "limit": 100,
                "fields": (
                    "items(track(name,artists,album,duration_ms,"
                    "external_ids,id,external_urls)),next"
                ),
            },
        )

        tracks = []
        for item in raw_items:
            track = item.get("track")
            if track and track.get("id"):
                metadata = self._track_to_metadata(track)
                if metadata:
                    tracks.append(metadata)

        logger.info(
            f"[Spotify] Fetched {len(tracks)} tracks from playlist {playlist_id}"
        )
        return tracks

    def fetch_user_playlists(self) -> list[dict]:
        """
        Fetch all of the authenticated user's playlists.
        Returns list of dicts with name, url, track_count, public, owner.
        """
        raw_items = self._paginate(
            "https://api.spotify.com/v1/me/playlists",
            params={"limit": 50},
        )

        playlists = []
        for item in raw_items:
            if not item:
                continue
            playlists.append({
                "id": item["id"],
                "name": item["name"],
                "track_count": (
                    item.get("tracks", {}).get("total")
                    or item.get("items", {}).get("total")
                    or 0
                ),
                "url": item["external_urls"]["spotify"],
                "owner": item.get("owner", {}).get("display_name", ""),
                "public": item.get("public", False),
            })

        logger.info(f"[Spotify] Found {len(playlists)} playlists")
        return playlists

    def fetch_saved_tracks(self) -> list[TrackMetadata]:
        """Fetch user's Liked Songs library."""
        raw_items = self._paginate(
            "https://api.spotify.com/v1/me/tracks",
            params={"limit": 50},
        )

        tracks = []
        for item in raw_items:
            track = item.get("track")
            if track and track.get("id"):
                metadata = self._track_to_metadata(track)
                if metadata:
                    tracks.append(metadata)

        logger.info(f"[Spotify] Fetched {len(tracks)} liked songs")
        return tracks

    def fetch_liked_count(self) -> int:
        """Return total number of Liked Songs."""
        r = self._get_with_retry(
            "https://api.spotify.com/v1/me/tracks",
            params={"limit": 1}
        )
        r.raise_for_status()
        return r.json().get("total", 0)

    def fetch_album_tracks(self, album_url: str) -> list[TrackMetadata]:
        """Fetch all tracks from an album URL or ID."""
        album_id = self._extract_playlist_id(album_url) # uses same logic
        raw_items = self._paginate(
            f"https://api.spotify.com/v1/albums/{album_id}/tracks",
            params={"limit": 50},
        )
        tracks = []
        for item in raw_items:
            if item.get("id"):
                metadata = self._track_to_metadata(item)
                if metadata:
                    tracks.append(metadata)
        return tracks

    def _track_to_metadata(self, track: dict) -> Optional[TrackMetadata]:
        """Convert raw Spotify track dict to Antra TrackMetadata."""
        try:
            artists = [a["name"] for a in track.get("artists", [])]
            isrc = track.get("external_ids", {}).get("isrc")
            duration_ms = track.get("duration_ms")
            album = track.get("album", {})
            images = album.get("images", [])
            artwork = images[0]["url"] if images else None
            release = album.get("release_date", "")

            return TrackMetadata(
                title=track["name"],
                artists=artists,
                album=album.get("name") or "",
                duration_ms=duration_ms,
                isrc=isrc,
                spotify_id=track.get("id"),
                artwork_url=artwork,
                release_date=release or None,
                release_year=int(release[:4]) if release else None,
                spotify_url=(
                    track.get("external_urls", {}).get("spotify")
                ),
            )
        except Exception as e:
            logger.debug(f"[Spotify] Failed to parse track: {e}")
            return None

    @staticmethod
    def _extract_playlist_id(url: str) -> str:
        """
        Extract playlist or album ID from URLs and URIs.

        Supports:
          - https://open.spotify.com/playlist/37i9dQZF1DX...
          - https://open.spotify.com/intl-es/playlist/37i9dQZF1DX...
          - spotify:playlist:37i9dQZF1DX...
          - https://open.spotify.com/album/0sNOF9WDwhWunNAHPD3Baj
          - spotify:album:0sNOF9WDwhWunNAHPD3Baj
          - raw ID: 37i9dQZF1DX...
        """
        normalized = _normalize_spotify_url(url)
        if "spotify.com/playlist/" in normalized:
            return normalized.split("playlist/")[1].split("?")[0].strip()
        elif "spotify.com/album/" in normalized:
            return normalized.split("album/")[1].split("?")[0].strip()
        elif "spotify:playlist:" in normalized:
            return normalized.split("spotify:playlist:")[1].strip()
        elif "spotify:album:" in normalized:
            return normalized.split("spotify:album:")[1].strip()
        elif "/" not in normalized and ":" not in normalized:
            return normalized
        raise ValueError(f"[Spotify] Cannot extract ID from: {url}")
