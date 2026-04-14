"""
Spotify Web API client — supports playlists, albums, tracks, and artists.
"""
import json
import logging
import re
import time
from typing import Optional
import spotipy
import requests
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth, SpotifyPKCE

from antra.core.models import TrackMetadata, SpotifyLibrary, SpotifyPlaylistSummary

logger = logging.getLogger(__name__)

# ── Spotify web-player TOTP credentials ──────────────────────────────────────
# Spotify's web player uses TOTP to obtain anonymous access tokens via
# open.spotify.com/api/token — no login or OAuth required.
# Secret and version extracted from the SpotiFLAC project (MIT licence).
_SP_TOTP_SECRET = (
    "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4D"
    "QOJUGQYTAMRRGA2TCMJSHE3TCMBY"
)
_SP_TOTP_VERSION = 61

# Persisted GraphQL query hashes for api-partner.spotify.com/pathfinder/v2/query
_GQL_ALBUM_HASH   = "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10"
_GQL_TRACK_HASH   = "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294"


class SpotifyResourceError(RuntimeError):
    """Raised when a Spotify resource cannot be fetched with the available token."""


_LOCALE_RE = re.compile(r"(spotify\.com/)intl-[a-z]+/")


def _normalize_spotify_url(url: str) -> str:
    """Strip locale prefix (e.g. /intl-es/) from Spotify URLs."""
    return _LOCALE_RE.sub(r"\1", url)


def _strip_id(url_or_id: str, type_hint: str) -> str:
    """Extract the bare Spotify ID from a URL of the given type."""
    url_or_id = _normalize_spotify_url(url_or_id)
    key = f"spotify.com/{type_hint}/"
    if key in url_or_id:
        part = url_or_id.split(key)[1]
        # Take only the ID segment — ignore trailing path parts like /discography/all
        return part.split("?")[0].split("/")[0].strip()
    # Could also be a spotify:type:id URI
    prefix = f"spotify:{type_hint}:"
    if url_or_id.startswith(prefix):
        return url_or_id[len(prefix):].strip()
    return url_or_id.strip()


def _detect_type(url_or_id: str) -> str:
    """Return 'playlist', 'album', 'track', or 'artist' based on the URL."""
    url_or_id = _normalize_spotify_url(url_or_id)
    for t in ("playlist", "album", "track", "artist"):
        if f"spotify.com/{t}/" in url_or_id or f"spotify:{t}:" in url_or_id:
            return t
    # Bare ID — assume playlist for backwards compatibility
    return "playlist"


class SpotifyClient:
    """Wraps Spotipy to extract normalized TrackMetadata.

    Supports:
      • Playlist URLs / IDs
      • Album URLs / IDs
      • Track URLs / IDs  (single track)
      • Artist URLs / IDs (top tracks)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        market: str = "",
        redirect_uri: str = "http://127.0.0.1:8888/callback",
        auth_storage_path: str = ".spotipyoauthcache",
        sp_dc: str = "",
    ):
        # Session state
        self._client_id = client_id.strip() if client_id else ""
        self._client_secret = client_secret.strip() if client_secret else ""
        self._market = market.strip().upper() or None
        self._redirect_uri = redirect_uri
        self._auth_storage_path = auth_storage_path
        self._sp_dc = sp_dc.strip()
        self._cache_path = auth_storage_path  # PKCE uses same path
        
        is_dummy_id = self._client_id in ("your_client_id_here", "dummy", "")
        if self._client_id and self._client_secret and not is_dummy_id:
            self.sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                ),
                retries=0,
                status_retries=0,
            )
        else:
            # Anonymous public client — PKCE will be used for private scopes
            self.sp = None
            logger.debug("SpotifyClient running without API credentials — will use PKCE for user access.")

        self._oauth_sp: Optional[spotipy.Spotify] = None  # lazy init

    def _oauth(self) -> spotipy.Spotify:
        """Return an authenticated Spotipy client using PKCE (no secret needed).
        Opens the browser for first-time login; silently refreshes forever after.
        """
        if self._oauth_sp and self._is_token_still_valid():
            return self._oauth_sp

        if not self._client_id:
            raise SpotifyResourceError(
                "Spotify access requires a Client ID. "
                "Run 'antra spotify login' to authenticate."
            )

        auth_manager = SpotifyPKCE(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            scope=(
                "playlist-read-private playlist-read-collaborative "
                "user-library-read user-follow-read"
            ),
            cache_path=self._auth_storage_path,
            open_browser=True,
        )
        self._oauth_sp = spotipy.Spotify(auth_manager=auth_manager, retries=0, status_retries=0)
        return self._oauth_sp

    def _is_token_still_valid(self) -> bool:
        """Quick check without making a network call."""
        try:
            return self._oauth_sp is not None
        except Exception:
            return False


    def login_user(self) -> bool:
        """Trigger the OAuth flow to log in a user."""
        try:
            sp = self._oauth()
            # Just try to get user info to confirm login
            sp.current_user()
            return True
        except Exception as e:
            logger.error(f"Spotify login failed: {e}")
            return False

    def logout_user(self):
        """Delete any cached OAuth tokens."""
        import os
        if os.path.exists(self._auth_storage_path):
            os.remove(self._auth_storage_path)
            logger.info("Logged out of Spotify (cache cleared).")
        self._oauth_sp = None

    def has_user_login(self) -> bool:
        """Return True if we have a valid cached PKCE token."""
        if not self._client_id:
            return False
        try:
            auth = SpotifyPKCE(
                client_id=self._client_id,
                redirect_uri=self._redirect_uri,
                scope=(
                    "playlist-read-private playlist-read-collaborative "
                    "user-library-read user-follow-read"
                ),
                cache_path=self._auth_storage_path,
            )
            token = auth.get_cached_token()
            return token is not None
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # User Library Access (requires OAuth)
    # ──────────────────────────────────────────────────────────────────────────

    def get_current_user_library(
        self,
        include_liked_songs: bool = True,
        include_saved_albums: bool = True,
        include_followed_artists: bool = True,
    ) -> SpotifyLibrary:
        """Fetch the authenticated user's profile and collection of playlists/albums."""
        sp = self._oauth()
        user = sp.current_user()
        user_id = user["id"]
        display_name = user.get("display_name") or user_id

        playlists: list[SpotifyPlaylistSummary] = []

        # 1. Liked Songs (Special marker)
        if include_liked_songs:
            try:
                saved = sp.current_user_saved_tracks(limit=1)
                total = saved.get("total", 0)
                if total > 0:
                    playlists.append(SpotifyPlaylistSummary(
                        id="me:liked",
                        name="Liked Songs",
                        owner=display_name,
                        total_tracks=total,
                        kind="collection",
                        url=f"https://open.spotify.com/collection/tracks",
                    ))
            except Exception as e:
                logger.debug(f"Failed to fetch Liked Songs count: {e}")

        # 2. Saved Albums
        if include_saved_albums:
            try:
                albums_resp = sp.current_user_saved_albums(limit=50)
                while albums_resp:
                    for item in albums_resp.get("items", []):
                        album = item.get("album", {})
                        if not album: continue
                        playlists.append(SpotifyPlaylistSummary(
                            id=album["id"],
                            name=album["name"],
                            owner=", ".join(a["name"] for a in album.get("artists", [])),
                            total_tracks=album.get("total_tracks", 0),
                            kind="album",
                            url=album.get("external_urls", {}).get("spotify"),
                        ))
                    if albums_resp.get("next"):
                        albums_resp = sp.next(albums_resp)
                    else:
                        albums_resp = None
            except Exception as e:
                logger.debug(f"Failed to fetch Saved Albums: {e}")

        # 3. User Playlists
        try:
            playlists_resp = sp.current_user_playlists(limit=50)
            while playlists_resp:
                for p in playlists_resp.get("items", []):
                    if not p: continue
                    playlists.append(SpotifyPlaylistSummary(
                        id=p["id"],
                        name=p["name"],
                        owner=p.get("owner", {}).get("display_name") or "Unknown",
                        total_tracks=p.get("tracks", {}).get("total", 0),
                        kind="playlist",
                        url=p.get("external_urls", {}).get("spotify"),
                        is_public=p.get("public"),
                        is_collaborative=p.get("collaborative", False),
                    ))
                if playlists_resp.get("next"):
                    playlists_resp = sp.next(playlists_resp)
                else:
                    playlists_resp = None
        except Exception as e:
            logger.debug(f"Failed to fetch user playlists: {e}")

        # 4. Followed Artists
        if include_followed_artists:
            try:
                followed_resp = sp.current_user_followed_artists(limit=50)
                while followed_resp:
                    artists = followed_resp.get("artists", {})
                    for a in artists.get("items", []):
                        if not a: continue
                        playlists.append(SpotifyPlaylistSummary(
                            id=a["id"],
                            name=f"Top Tracks: {a['name']}",
                            owner=a["name"],
                            total_tracks=10, # Top tracks is usually 10
                            kind="artist",
                            url=a.get("external_urls", {}).get("spotify"),
                        ))
                    if artists.get("next"):
                        followed_resp = sp.next(artists)
                    else:
                        followed_resp = None
            except Exception as e:
                logger.debug(f"Failed to fetch followed artists: {e}")

        return SpotifyLibrary(
            user_id=user_id,
            display_name=display_name,
            playlists=playlists,
        )

    def get_library_selection_tracks(self, selection: SpotifyPlaylistSummary) -> list[TrackMetadata]:
        """Fetch all tracks for a library selection (playlist, liked songs, or album)."""
        if selection.kind == "playlist":
            return self._fetch_playlist(selection.id)
        elif selection.kind == "album":
            return self._fetch_album(selection.id)
        elif selection.kind == "collection":
            if selection.id == "me:liked":
                return self._fetch_liked_songs()
        
        raise ValueError(f"Unsupported selection kind: {selection.kind}")

    def _fetch_liked_songs(self) -> list[TrackMetadata]:
        """Fetch every track in the user's Liked Songs collection."""
        sp = self._oauth()
        tracks: list[TrackMetadata] = []
        limit = 50
        offset = 0
        
        logger.info("Fetching Liked Songs...")
        while True:
            response = sp.current_user_saved_tracks(limit=limit, offset=offset)
            items = response.get("items", [])
            if not items:
                break
                
            for item in items:
                track = item.get("track")
                if not track or track.get("id") is None:
                    continue
                meta = self._parse_track(track)
                if meta:
                    meta.playlist_name = "Liked Songs"
                    meta.playlist_position = len(tracks) + 1
                    tracks.append(meta)
            
            logger.info(f"  Fetched {len(tracks)} Liked Songs so far...")
            if not response.get("next"):
                break
            offset += limit
            time.sleep(0.1)
            
        return tracks

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    def get_playlist_tracks(self, url_or_id: str) -> list[TrackMetadata]:
        """Fetch tracks from any Spotify URL (playlist, album, track, artist)."""
        kind = _detect_type(url_or_id)
        logger.info(f"Fetching {kind}: {url_or_id}")

        if kind == "playlist":
            return self._fetch_playlist(url_or_id)
        elif kind == "album":
            return self._fetch_album(url_or_id)
        elif kind == "track":
            return self._fetch_track(url_or_id)
        elif kind == "artist":
            return self._fetch_artist_top(url_or_id)
        else:
            raise ValueError(f"Unsupported Spotify URL type: {url_or_id}")

    # ──────────────────────────────────────────────────────────────────────────
    # Fetchers per type
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_playlist(self, url_or_id: str) -> list[TrackMetadata]:
        playlist_id = _strip_id(url_or_id, "playlist")
        tracks: list[TrackMetadata] = []
        limit = 100
        playlist_name = "Unknown Playlist"

        # Resolve which authenticated client to use without mutating self.sp.
        # Try the credentials client first; escalate to OAuth for private
        # playlists; fall back to the public embed scraper as a last resort.
        active_sp = self._resolve_playlist_client(playlist_id)
        if active_sp is None:
            fallback_tracks = self._fetch_public_playlist_embed(playlist_id)
            if fallback_tracks:
                logger.info("Using public embed fallback for playlist.")
                return fallback_tracks
            raise self._playlist_access_error(
                playlist_id, RuntimeError("All authentication methods failed")
            )

        playlist_name = self._fetch_playlist_name(active_sp, playlist_id)

        # Paginate through ALL pages — Spotify returns at most `limit` items
        # per request.  We keep advancing `offset` until the response contains
        # no `next` URL, which is the reliable end-of-results signal.
        offset = 0
        while True:
            try:
                response = active_sp.playlist_items(
                    playlist_id,
                    offset=offset,
                    limit=limit,
                    market=self._market,
                    additional_types=["track"],
                )
            except spotipy.SpotifyException as e:
                logger.warning(f"[Spotify] playlist_items failed at offset {offset}: {e}")
                break

            items = response.get("items", [])
            if not items:
                break

            for item in items:
                track = item.get("item") or item.get("track")
                if not track or track.get("id") is None:
                    continue
                meta = self._parse_track(track)
                if meta:
                    meta.playlist_name = playlist_name
                    meta.playlist_position = len(tracks) + 1
                    tracks.append(meta)

            logger.info(f"  Fetched {len(tracks)} tracks so far...")

            # `next` is None when there are no more pages
            if not response.get("next"):
                break

            offset += limit
            time.sleep(0.1)  # be a polite API client

        logger.info(f"Total tracks in playlist: {len(tracks)}")
        return tracks

    def _resolve_playlist_client(self, playlist_id: str) -> "Optional[spotipy.Spotify]":
        """
        Return the right Spotipy client for this playlist without side-effects.

        Try order:
          1. Client Credentials (works for all public playlists, no browser)
          2. OAuth              (required for private / collaborative playlists)
          3. None               (caller should use the embed fallback)
        """
        if not self.sp:
            return None

        try:
            # A lightweight probe — just fetch the first item to confirm access.
            self.sp.playlist_items(
                playlist_id,
                offset=0,
                limit=1,
                additional_types=["track"],
            )
            return self.sp
        except spotipy.SpotifyException as e:
            code = str(e)
            lowered = code.lower()
            if "active premium subscription required for the owner of the app" in lowered:
                logger.debug(
                    "Spotify playlist API is blocked for the current app credentials; "
                    "using public fallback instead of OAuth."
                )
                return None
            if "401" in code:
                logger.info("Playlist requires user login — trying OAuth...")
                oauth_sp = self._oauth()
                try:
                    oauth_sp.playlist_items(
                        playlist_id,
                        offset=0,
                        limit=1,
                        additional_types=["track"],
                    )
                    return oauth_sp
                except spotipy.SpotifyException as oauth_err:
                    logger.warning(f"OAuth also failed for playlist {playlist_id}: {oauth_err}")
                    return None
            if "403" in code:
                logger.debug(
                    "Spotify playlist API denied access for the current app credentials; "
                    "using public fallback."
                )
                return None
            # Non-auth error (404, 5xx, etc.) — try embed fallback
            logger.warning(f"Playlist API error for {playlist_id}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Playlist client validation failed (likely invalid credentials): {e}")
            return None

    def _fetch_playlist_name(self, sp: spotipy.Spotify, playlist_id: str) -> str:
        try:
            playlist = sp.playlist(playlist_id, fields="name")
            return playlist.get("name", "Unknown Playlist")
        except Exception:
            return "Unknown Playlist"

    def _get_with_retry_public(self, url: str) -> requests.Response:
        for attempt in range(4):
            try:
                resp = requests.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    timeout=20
                )
                if resp.status_code in (500, 502, 503, 504):
                    logger.debug(f"[Spotify] {resp.status_code} for {url}, retrying...")
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                if attempt == 3:
                    raise
                logger.debug(f"[Spotify] Network exception {e} for {url}, retrying...")
                time.sleep(2 ** attempt)
        raise requests.RequestException(f"Failed to fetch {url} after 4 attempts")

    def _fetch_public_playlist_embed(self, playlist_id: str) -> list[TrackMetadata]:
        """
        Fallback for public playlists when the Web API blocks playlist_items.

        Strategy:
          1. Scrape the embed page to get the playlist title and the anonymous
             access token embedded in the HTML __NEXT_DATA__.
          2. Use the Spotify Web Player GraphQL API to fetch the full playlist
             metadata (this bypasses the 100-track limit and doesn't require Premium).
        """
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        
        try:
            # ── Step 1: embed payload for Token & Title ───────────────────
            embed_response = self._get_with_retry_public(embed_url)
            data = self._extract_next_data(embed_response.text)
            
            entity = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("data", {})
                    .get("entity", {})
            )
            playlist_title = entity.get("title") or entity.get("name") or "Unknown Playlist"
            
            token = self._get_anonymous_access_token(playlist_id)
            if not token:
                logger.warning(f"Could not extract token from embed page for {playlist_id}")
                return []

            # ── Step 2: GraphQL pagination for Full Metadata ──────────────
            tracks: list[TrackMetadata] = []
            offset = 0
            limit = 1000
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "App-Platform": "WebPlayer",
                "Spotify-App-Version": "1.0.0",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            
            while True:
                payload = {
                    "variables": {
                        "uri": f"spotify:playlist:{playlist_id}",
                        "offset": offset,
                        "limit": limit,
                        "enableWatchFeedEntrypoint": False
                    },
                    "operationName": "fetchPlaylist",
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"
                        }
                    }
                }
                
                resp = requests.post(
                    "https://api-partner.spotify.com/pathfinder/v2/query",
                    headers=headers,
                    json=payload,
                    timeout=15,
                )
                
                if not resp.ok:
                    logger.debug(f"[Spotify] Partner GraphQL API returned {resp.status_code} at offset {offset}")
                    break
                    
                data = resp.json()
                playlist_data = data.get("data", {}).get("playlistV2", {})
                content = playlist_data.get("content", {})
                items = content.get("items", [])
                
                if not items:
                    break
                    
                for item in items:
                    v2_data = item.get("itemV2", {}).get("data", {})
                    if not v2_data:
                        continue
                        
                    uri = v2_data.get("uri", "")
                    if not uri.startswith("spotify:track:"):
                        continue
                        
                    tid = uri.split(":")[-1]
                    title = v2_data.get("name") or "Unknown Track"
                    artists = [a.get("profile", {}).get("name") for a in v2_data.get("artists", {}).get("items", []) if a.get("profile", {}).get("name")]
                    if not artists: artists = ["Unknown Artist"]

                    album_of_track = v2_data.get("albumOfTrack", {})
                    album = album_of_track.get("name") or "Unknown Album"
                    # Extract album_id from its URI so batch_enrich_album_data can fill artwork/genres
                    album_uri = album_of_track.get("uri", "")
                    album_id = album_uri.split(":")[-1] if album_uri.startswith("spotify:album:") else None
                    # Extract artwork from album coverArt images
                    cover_sources = album_of_track.get("coverArt", {}).get("sources", [])
                    artwork_url = cover_sources[0].get("url") if cover_sources else None
                    duration_ms = v2_data.get("trackDuration", {}).get("totalMilliseconds")
                    release_date = album_of_track.get("date", {}).get("isoString", "") if isinstance(album_of_track.get("date"), dict) else ""
                    release_year = int(release_date[:4]) if release_date and release_date[:4].isdigit() else None

                    track_meta = TrackMetadata(
                        title=title,
                        artists=artists,
                        album=album,
                        album_id=album_id,
                        artwork_url=artwork_url,
                        release_year=release_year,
                        release_date=release_date or None,
                        playlist_name=playlist_title,
                        playlist_position=len(tracks) + 1,
                        duration_ms=int(duration_ms) if duration_ms else None,
                        track_number=v2_data.get("trackNumber"),
                        disc_number=v2_data.get("discNumber"),
                        spotify_id=tid
                    )
                    tracks.append(track_meta)
                    
                total_count = content.get("totalCount", 0)
                if len(items) < limit or offset + len(items) >= total_count:
                    break
                    
                offset += limit
                time.sleep(0.15)
                
            logger.info(f"Total tracks in public playlist fallback: {len(tracks)}")
            return tracks

        except Exception as e:
            logger.warning(f"Public playlist fallback failed for {playlist_id}: {e}")
            return []

    def _fetch_all_playlist_track_ids(self, playlist_id: str) -> list[str]:
        """
        Return every track ID in a playlist, regardless of size or access tier.

        Try order:
          1. Public Web API — works for most playlists, paginated 100 at a time.
          2. Spotify internal partner API — works for editorial / 403 playlists
             (e.g. "Today's Top Hits") using an anonymous access token extracted
             from open.spotify.com, the same token the web player uses. No
             Premium subscription or OAuth login required.
        """
        ids = self._fetch_track_ids_via_web_api(playlist_id)
        if ids:
            return ids

        logger.debug(
            f"[Spotify] Web API blocked for {playlist_id} — "
            "falling back to internal partner API"
        )
        return self._fetch_track_ids_via_partner_api(playlist_id)

    def _fetch_track_ids_via_web_api(self, playlist_id: str) -> list[str]:
        """
        Paginate the public playlist_items endpoint.
        Returns [] immediately on 403/401 so the caller can try the partner API.
        """
        seen: set[str] = set()
        ids: list[str] = []
        offset = 0
        limit = 100

        # Find a working client (credentials first, then OAuth)
        clients = [self.sp]
        if self._oauth_sp and self._oauth_sp is not self.sp:
            clients.append(self._oauth_sp)

        active: Optional[spotipy.Spotify] = None
        for client in clients:
            try:
                client.playlist_items(
                    playlist_id, offset=0, limit=1, additional_types=["track"]
                )
                active = client
                break
            except spotipy.SpotifyException as e:
                if "403" in str(e) or "401" in str(e):
                    # Auth/access blocked — stop trying clients, go to partner API
                    return []
                # Non-auth error (404, 5xx) — try next client
                continue
            except Exception:
                continue

        if active is None:
            return []

        while True:
            try:
                response = active.playlist_items(
                    playlist_id,
                    offset=offset,
                    limit=limit,
                    fields="items(track(id)),next",
                    additional_types=["track"],
                )
            except Exception as e:
                logger.debug(f"[Spotify] Web API pagination failed at offset {offset}: {e}")
                break

            for item in response.get("items", []):
                tid = (item.get("track") or {}).get("id")
                if tid and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)

            if not response.get("next"):
                break
            offset += limit
            time.sleep(0.1)

        return ids

    def _get_anonymous_access_token(self, _unused: str = "") -> Optional[str]:
        """
        Return an anonymous Bearer token for Spotify's internal partner API.
        Tries TOTP generation first (reliable); falls back to embed-page HTML
        scraping if pyotp is not installed.
        """
        # ── Primary: TOTP-based token (no HTML scraping needed) ──────────────
        token = self._get_totp_access_token()
        if token:
            return token

        # ── Fallback: scrape the embed page for the token ─────────────────────
        try:
            resp = self._get_with_retry_public(
                "https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text,
            )
            if match:
                data = json.loads(match.group(1))

                def _find_token(obj):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k == "accessToken":
                                return v
                            elif isinstance(v, (dict, list)):
                                res = _find_token(v)
                                if res:
                                    return res
                    elif isinstance(obj, list):
                        for item in obj:
                            res = _find_token(item)
                            if res:
                                return res
                    return None

                token = _find_token(data)
                if token:
                    return token
        except Exception as e:
            logger.debug(f"[Spotify] Embed-page token scrape failed: {e}")

        logger.debug("[Spotify] Could not obtain anonymous access token")
        return None

    def _get_totp_access_token(self) -> Optional[str]:
        """
        Generate a Spotify anonymous access token using the web player's TOTP
        mechanism. Works without any Spotify credentials or scraping.
        Requires: pip install pyotp
        """
        try:
            import pyotp
            totp = pyotp.TOTP(_SP_TOTP_SECRET)
            code = totp.now()
            resp = requests.get(
                "https://open.spotify.com/api/token",
                params={
                    "reason": "init",
                    "productType": "web-player",
                    "totp": code,
                    "totpVer": str(_SP_TOTP_VERSION),
                    "totpServer": code,
                },
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/145.0.0.0 Safari/537.36"
                    )
                },
                timeout=10,
            )
            if resp.ok:
                token = resp.json().get("accessToken")
                if token:
                    logger.debug("[Spotify] TOTP anonymous token acquired")
                    return token
        except ImportError:
            logger.debug("[Spotify] pyotp not installed — TOTP token unavailable")
        except Exception as e:
            logger.debug(f"[Spotify] TOTP token failed: {e}")
        return None

    def _fetch_track_ids_via_partner_api(self, playlist_id: str) -> list[str]:
        """
        Use Spotify's internal GraphQL API (the endpoint the web player calls) to
        page through a playlist. This allows fetching up to 1000 tracks per request
        and avoids the public REST API constraints.
        Only needs an anonymous token from open.spotify.com.
        """
        token = self._get_anonymous_access_token(playlist_id)
        if not token:
            logger.debug("[Spotify] No anonymous token available — partner API skipped")
            return []

        seen: set[str] = set()
        ids: list[str] = []
        offset = 0
        limit = 1000

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "App-Platform": "WebPlayer",
            "Spotify-App-Version": "1.0.0",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        while True:
            try:
                payload = {
                    "variables": {
                        "uri": f"spotify:playlist:{playlist_id}",
                        "offset": offset,
                        "limit": limit,
                        "enableWatchFeedEntrypoint": False
                    },
                    "operationName": "fetchPlaylist",
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"
                        }
                    }
                }
                
                resp = requests.post(
                    "https://api-partner.spotify.com/pathfinder/v2/query",
                    headers=headers,
                    json=payload,
                    timeout=15,
                )

                if resp.status_code == 401:
                    # Token expired mid-pagination — refresh once and retry
                    token = self._get_anonymous_access_token(playlist_id)
                    if not token:
                        break
                    headers["Authorization"] = f"Bearer {token}"
                    continue

                if not resp.ok:
                    logger.debug(
                        f"[Spotify] Partner GraphQL API returned {resp.status_code} "
                        f"at offset {offset}: {resp.text[:200]}"
                    )
                    break

                data = resp.json()
                playlist_data = data.get("data", {}).get("playlistV2", {})
                content = playlist_data.get("content", {})
                items = content.get("items", [])

            except Exception as e:
                logger.debug(f"[Spotify] Partner GraphQL API error at offset {offset}: {e}")
                break

            if not items:
                break

            for item in items:
                def extract_track_id(node):
                    if isinstance(node, dict):
                        uri = node.get("uri")
                        if uri and str(uri).startswith("spotify:track:"):
                            return str(uri).split(":")[-1]
                        for k, v in node.items():
                            if k == "id" and node.get("type", "").upper() == "TRACK":
                                return v
                            if isinstance(v, (dict, list)):
                                res = extract_track_id(v)
                                if res:
                                    return res
                    elif isinstance(node, list):
                        for el in node:
                            res = extract_track_id(el)
                            if res:
                                return res
                    return None

                tid = extract_track_id(item)
                if tid and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)

            total_count = content.get("totalCount", 0)
            if len(items) < limit or offset + len(items) >= total_count:
                break
            
            offset += limit
            time.sleep(0.15)

        logger.debug(f"[Spotify] Partner GraphQL API returned {len(ids)} track IDs")
        return ids

    def _fetch_tracks_batch(self, track_ids: list[str]) -> dict[str, dict]:
        full_tracks: dict[str, dict] = {}
        clients = [self.sp]
        if self._oauth_sp and self._oauth_sp is not self.sp:
            clients.append(self._oauth_sp)

        for start in range(0, len(track_ids), 50):
            chunk = track_ids[start:start + 50]
            missing_ids = [track_id for track_id in chunk if track_id not in full_tracks]
            for client in clients:
                if not missing_ids:
                    break
                try:
                    response = client.tracks(missing_ids, market=self._market)
                    for track in response.get("tracks", []):
                        if track and track.get("id"):
                            full_tracks[track["id"]] = track
                    missing_ids = [track_id for track_id in chunk if track_id not in full_tracks]
                except Exception as e:
                    logger.debug(f"Batch track lookup failed for playlist fallback chunk: {e}")
        return full_tracks

    @staticmethod
    def _extract_next_data(html: str) -> dict:
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not match:
            raise ValueError("Could not find Spotify embed data payload")
        return json.loads(match.group(1))

    @classmethod
    def _collect_public_track_items(cls, data: dict) -> list[dict]:
        """Collect track-like objects from the public page payload.

        Spotify's public payload shape varies across playlist pages. We start
        with the explicit entity.trackList when available, then recursively
        walk the payload to find additional track objects that expose a
        spotify:track: URI. Results are deduplicated in first-seen order.
        """
        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        candidates = []
        if isinstance(entity.get("trackList"), list):
            candidates.extend(entity["trackList"])

        seen_uris = {
            item.get("uri")
            for item in candidates
            if isinstance(item, dict) and str(item.get("uri", "")).startswith("spotify:track:")
        }

        for item in cls._iter_public_track_items(data):
            uri = item.get("uri")
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            candidates.append(item)

        return candidates

    @classmethod
    def _iter_public_track_items(cls, value):
        if isinstance(value, dict):
            uri = value.get("uri", "")
            if isinstance(uri, str) and uri.startswith("spotify:track:"):
                yield value
            for nested in value.values():
                yield from cls._iter_public_track_items(nested)
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_public_track_items(item)

    @staticmethod
    def _extract_track_ids_from_html(html: str) -> list[str]:
        seen = set()
        track_ids: list[str] = []
        for match in re.finditer(r"spotify:track:([A-Za-z0-9]+)", html):
            track_id = match.group(1)
            if track_id in seen:
                continue
            seen.add(track_id)
            track_ids.append(track_id)
        return track_ids

    def _playlist_access_error(self, playlist_id: str, error: Exception) -> SpotifyResourceError:
        message = str(error)
        if "404" in message:
            detail = (
                f"Spotify playlist {playlist_id} is not available to the current account. "
                "This commonly happens with personalized or region-limited playlists. "
                "Try setting SPOTIFY_MARKET to your account country code, such as IN or US, "
                "then delete .spotipyoauthcache and re-authenticate."
            )
        elif "401" in message or "403" in message:
            detail = (
                f"Spotify playlist {playlist_id} requires user access that the current token "
                "does not have."
            )
        else:
            detail = f"Spotify playlist {playlist_id} could not be fetched."
        return SpotifyResourceError(detail)

    def _fetch_album(self, url_or_id: str) -> list[TrackMetadata]:
        album_id = _strip_id(url_or_id, "album")
        if not self.sp:
            fallback_tracks = self._fetch_public_album_page(album_id)
            if fallback_tracks:
                return fallback_tracks
            raise SpotifyResourceError(f"Anonymous album fallback failed for {album_id}. Check config.")

        try:
            # Fetch full album for metadata
            album_data = self.sp.album(album_id, market=self._market)
            album_name = album_data.get("name", "Unknown Album")
            release_date = album_data.get("release_date", "")
            release_year = int(release_date[:4]) if release_date else None
            images = album_data.get("images", [])
            artwork_url = images[0]["url"] if images else None
            total_tracks = album_data.get("total_tracks")
            album_genres = album_data.get("genres", [])
            album_artist_names = [a["name"] for a in album_data.get("artists", []) if a.get("name")]
        except Exception as e:
            logger.warning(f"Spotify album API unavailable or credentials invalid for {album_id}: {e}")
            fallback_tracks = self._fetch_public_album_page(album_id)
            if fallback_tracks:
                return fallback_tracks
            raise

        tracks: list[TrackMetadata] = []
        offset = 0
        limit = 50

        while True:
            response = self.sp.album_tracks(album_id, offset=offset, limit=limit, market=self._market)
            items = response.get("items", [])
            if not items:
                break

            for t in items:
                if not t.get("id"):
                    continue
                artists = [a["name"] for a in t.get("artists", [])]
                tracks.append(TrackMetadata(
                    title=t["name"],
                    artists=artists,
                    album=album_name,
                    album_artists=album_artist_names,
                    release_year=release_year,
                    release_date=release_date,
                    track_number=t.get("track_number"),
                    disc_number=t.get("disc_number"),
                    total_tracks=total_tracks,
                    duration_ms=t.get("duration_ms"),
                    isrc=None,
                    spotify_id=t["id"],
                    album_id=album_id,
                    artwork_url=artwork_url,
                    genres=album_genres,
                ))

            logger.info(f"  Fetched {len(tracks)} album tracks so far...")
            if not response.get("next"):
                break
            offset += limit
            time.sleep(0.1)

        logger.info(f"Total tracks in album: {len(tracks)}")
        return tracks

    def search_track(self, query: str) -> Optional[TrackMetadata]:
        """Search Spotify for a track by string query (e.g. 'Artist Title'),
        falling back to public iTunes Search API if anonymous/unauthenticated.
        """
        if self.sp:
            try:
                results = self.sp.search(q=query, type="track", limit=1, market=self._market)
                tracks = results.get("tracks", {}).get("items", [])
                if tracks:
                    return self._parse_track(tracks[0])
            except spotipy.SpotifyException as e:
                logger.debug(f"[Spotify] Search API error for '{query}': {e}")
            except Exception as e:
                logger.debug(f"[Spotify] Search failed for '{query}': {e}")

        # Fallback to iTunes Search API (100% public, no auth, good metadata)
        return self._search_track_itunes(query)

    def _get_anonymous_token(self) -> Optional[str]:
        """
        Fetch a short-lived anonymous Spotify token from the web player endpoint.
        No credentials required — same mechanism browsers use to load open.spotify.com.
        """
        try:
            resp = requests.get(
                "https://open.spotify.com/get_access_token",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                },
                params={"reason": "transport", "productType": "web_player"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("accessToken")
        except Exception as e:
            logger.debug(f"[Spotify] Anonymous token fetch failed: {e}")
            return None

    def search_artists(self, query: str, limit: int = 8) -> list[dict]:
        """Search for artists by name. Returns scored list for the artist search UI.

        Each result: {artist_id, name, artwork_url, genres, followers, match_score, profile_url}
        match_score is 0.0–1.0, sorted descending.
        Falls back to iTunes artist search if Spotify credentials aren't available.
        """
        from antra.utils.matching import normalize, string_similarity

        results: list[dict] = []

        # Try Spotipy first
        if self.sp:
            try:
                raw = self.sp.search(q=query, type="artist", limit=limit, market=self._market)
                artists = raw.get("artists", {}).get("items", [])
                for a in artists:
                    name = a.get("name", "")
                    images = a.get("images", [])
                    artwork = images[0]["url"] if images else None
                    artist_id = a.get("id", "")
                    score = string_similarity(normalize(query), normalize(name))
                    results.append({
                        "artist_id": artist_id,
                        "name": name,
                        "artwork_url": artwork,
                        "genres": a.get("genres", []),
                        "followers": a.get("followers", {}).get("total"),
                        "match_score": round(score, 3),
                        "profile_url": f"https://open.spotify.com/artist/{artist_id}",
                        "source": "spotify",
                    })
                results.sort(key=lambda x: x["match_score"], reverse=True)
                return results
            except Exception as e:
                logger.debug(f"[Spotify] Artist search failed: {e}")

        # No Spotipy credentials — get an anonymous token via the embed-page scraper.
        # _get_anonymous_access_token() is already proven reliable (used for playlists).
        token = self._get_anonymous_access_token()
        if token:
            try:
                resp = requests.get(
                    "https://api.spotify.com/v1/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": query, "type": "artist", "limit": limit},
                    timeout=10,
                )
                resp.raise_for_status()
                artists = resp.json().get("artists", {}).get("items", [])
                for a in artists:
                    name = a.get("name", "")
                    images = a.get("images", [])
                    artwork = images[0]["url"] if images else None
                    artist_id = a.get("id", "")
                    score = string_similarity(normalize(query), normalize(name))
                    results.append({
                        "artist_id": artist_id,
                        "name": name,
                        "artwork_url": artwork,
                        "genres": a.get("genres", []),
                        "followers": a.get("followers", {}).get("total"),
                        "match_score": round(score, 3),
                        "profile_url": f"https://open.spotify.com/artist/{artist_id}",
                        "source": "spotify",
                    })
                results.sort(key=lambda x: x["match_score"], reverse=True)
                if results:
                    return results
            except Exception as e:
                logger.debug(f"[Spotify] Anonymous artist search failed: {e}")

        # Nothing worked — return empty (caller decides whether to surface an error)
        return []

    def _search_artists_itunes(self, query: str, limit: int = 8) -> list[dict]:
        """Artist search via public iTunes Search API — no credentials required."""
        from antra.utils.matching import normalize, string_similarity
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "entity": "musicArtist", "limit": limit, "media": "music"},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("results", [])
            results = []
            for a in items:
                name = a.get("artistName", "")
                artist_id = str(a.get("artistId", ""))
                score = string_similarity(normalize(query), normalize(name))
                results.append({
                    "artist_id": artist_id,
                    "name": name,
                    "artwork_url": None,
                    "genres": [a["primaryGenreName"]] if a.get("primaryGenreName") else [],
                    "followers": None,
                    "match_score": round(score, 3),
                    "profile_url": f"https://music.apple.com/us/artist/{artist_id}",
                    "source": "apple",
                })
            results.sort(key=lambda x: x["match_score"], reverse=True)
            return results
        except Exception as e:
            logger.debug(f"[iTunes] Artist search fallback failed: {e}")
            return []

    def _search_track_itunes(self, query: str) -> Optional[TrackMetadata]:
        url = "https://itunes.apple.com/search"
        params = {
            "term": query,
            "entity": "song",
            "limit": 1,
            "media": "music"
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            if not results:
                return None
            
            item = results[0]
            track_name = item.get("trackName") or item.get("collectionName") or "Unknown Track"
            artist_name = item.get("artistName") or "Unknown Artist"
            album_name = item.get("collectionName") or "Unknown Album"
            release_date = item.get("releaseDate", "")
            release_year = int(release_date[:4]) if release_date else None
            duration_ms = item.get("trackTimeMillis")
            artwork_url = item.get("artworkUrl100")
            if artwork_url:
                artwork_url = artwork_url.replace("100x100bb", "600x600bb")
                
            return TrackMetadata(
                title=track_name,
                artists=[artist_name],
                album=album_name,
                release_year=release_year,
                release_date=release_date,
                track_number=item.get("trackNumber"),
                disc_number=item.get("discNumber"),
                total_tracks=item.get("trackCount"),
                duration_ms=duration_ms,
                isrc=None,
                spotify_id=None,
                album_id=None,
                artwork_url=artwork_url,
                genres=[item.get("primaryGenreName")] if item.get("primaryGenreName") else [],
            )
        except Exception as e:
            logger.debug(f"[iTunes] Search API fallback failed for '{query}': {e}")
            return None

    def _fetch_public_album_page(self, album_id: str) -> list[TrackMetadata]:
        url = f"https://open.spotify.com/album/{album_id}"
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            html = response.text
            data = self._extract_next_data(html)
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            album_name = entity.get("name") or entity.get("title") or self._extract_meta_content(html, "og:title") or "Unknown Album"
            artwork_url = self._extract_meta_content(html, "og:image")
            release_date = (
                entity.get("releaseDate")
                or entity.get("release_date")
                or entity.get("date")
                or ""
            )
            release_year = int(release_date[:4]) if str(release_date)[:4].isdigit() else None
            track_items = self._collect_public_track_items(data)

            tracks: list[TrackMetadata] = []
            for index, item in enumerate(track_items, start=1):
                track_uri = item.get("uri", "")
                if not track_uri.startswith("spotify:track:"):
                    continue
                title = item.get("title") or item.get("name") or "Unknown Track"
                artists = self._parse_public_artists(item.get("subtitle")) or ["Unknown Artist"]
                duration_ms = item.get("duration") or item.get("durationMs")
                tracks.append(
                    TrackMetadata(
                        title=title,
                        artists=artists,
                        album=album_name,
                        release_year=release_year,
                        release_date=release_date or None,
                        track_number=index,
                        total_tracks=len(track_items),
                        duration_ms=duration_ms,
                        spotify_id=track_uri.split(":")[-1],
                        album_id=album_id,
                        artwork_url=artwork_url,
                    )
                )

            logger.info(f"Total tracks in public album fallback: {len(tracks)}")
            return tracks
        except Exception as e:
            logger.debug(f"Public album fallback failed for {album_id}: {e}")
            return []

    def _fetch_album_via_partner_api(self, album_id: str) -> list[TrackMetadata]:
        """
        Fetch album tracks using Spotify's internal partner GraphQL API with an
        anonymous TOTP token. More reliable than HTML scraping; returns full
        track list including disc/track numbers and durations.
        No Spotify credentials required.
        """
        token = self._get_anonymous_access_token()
        if not token:
            return []

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "App-Platform": "WebPlayer",
            "Spotify-App-Version": "1.0.0",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        }

        all_items: list = []
        offset = 0
        limit = 300
        album_meta: dict = {}

        while True:
            payload = {
                "variables": {
                    "uri": f"spotify:album:{album_id}",
                    "locale": "",
                    "offset": offset,
                    "limit": limit,
                },
                "operationName": "getAlbum",
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": _GQL_ALBUM_HASH,
                    }
                },
            }
            try:
                resp = requests.post(
                    "https://api-partner.spotify.com/pathfinder/v2/query",
                    headers=headers,
                    json=payload,
                    timeout=20,
                )
                if not resp.ok:
                    logger.debug(
                        f"[Spotify] Partner album API {resp.status_code} for {album_id}: {resp.text[:200]}"
                    )
                    break
                data = resp.json()
            except Exception as e:
                logger.debug(f"[Spotify] Partner album API error: {e}")
                break

            album_union = data.get("data", {}).get("albumUnion", {})
            if not album_meta:
                # Capture album-level fields on first page
                artists_items = (
                    album_union.get("artists", {}).get("items", [])
                )
                album_meta = {
                    "name": album_union.get("name", ""),
                    "artists": [
                        a.get("profile", {}).get("name", "")
                        for a in artists_items
                        if isinstance(a, dict)
                    ],
                    "release_date": (
                        album_union.get("date", {}).get("isoString", "")
                        or album_union.get("date", {}).get("year", "")
                    ),
                    "artwork_url": self._pick_best_cover(
                        album_union.get("coverArt", {}).get("sources", [])
                    ),
                    "total_tracks": album_union.get("tracksV2", {}).get("totalCount"),
                }
                # Strip time portion from ISO date
                if album_meta["release_date"] and "T" in album_meta["release_date"]:
                    album_meta["release_date"] = album_meta["release_date"].split("T")[0]

            tracks_v2 = album_union.get("tracksV2", {})
            items = tracks_v2.get("items", [])
            if not items:
                break
            all_items.extend(items)
            total = tracks_v2.get("totalCount", 0)
            if len(all_items) >= total or len(items) < limit:
                break
            offset += limit

        if not all_items:
            return []

        album_name = album_meta.get("name", "")
        album_artists = album_meta.get("artists", [])
        release_date = album_meta.get("release_date", "")
        release_year = int(release_date[:4]) if release_date and release_date[:4].isdigit() else None
        artwork_url = album_meta.get("artwork_url")
        total_tracks = album_meta.get("total_tracks") or len(all_items)

        result: list[TrackMetadata] = []
        for idx, item in enumerate(all_items, start=1):
            track = item.get("track") if isinstance(item.get("track"), dict) else item
            if not track or not track.get("name"):
                continue
            try:
                track_uri = track.get("uri", "")
                spotify_id = track_uri.split(":")[-1] if ":" in track_uri else track.get("id")
                artist_items = track.get("artists", {}).get("items", [])
                artists = [
                    a.get("profile", {}).get("name", "")
                    for a in artist_items
                    if isinstance(a, dict)
                ]
                duration_ms = track.get("duration", {}).get("totalMilliseconds")
                result.append(TrackMetadata(
                    title=track.get("name", ""),
                    artists=artists or album_artists,
                    album=album_name,
                    album_artists=album_artists,
                    album_id=album_id,
                    release_date=release_date or None,
                    release_year=release_year,
                    track_number=track.get("trackNumber") or idx,
                    disc_number=track.get("discNumber") or 1,
                    total_tracks=total_tracks,
                    duration_ms=int(duration_ms) if duration_ms else None,
                    artwork_url=artwork_url,
                    spotify_id=spotify_id,
                ))
            except Exception as e:
                logger.debug(f"[Spotify] Skipping partner API album track: {e}")

        logger.info(f"[Spotify] Partner API: {len(result)} tracks for album {album_id}")
        return result

    @staticmethod
    def _pick_best_cover(sources: list) -> Optional[str]:
        """Return the largest cover art URL from a list of {url, width, height} dicts."""
        if not sources:
            return None
        try:
            best = max(
                (s for s in sources if isinstance(s, dict) and s.get("url")),
                key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
                default=None,
            )
            return best["url"] if best else None
        except Exception:
            return sources[0].get("url") if sources else None

    def _fetch_track(self, url_or_id: str) -> list[TrackMetadata]:
        track_id = _strip_id(url_or_id, "track")
        if not self.sp:
            meta = self._fetch_public_track_page(track_id)
            return [meta] if meta else []

        try:
            track = self.sp.track(track_id, market=self._market)
            meta = self._parse_track(track)
            return [meta] if meta else []
        except Exception as e:
            logger.warning(f"Spotify track API unavailable or credentials invalid for {track_id}: {e}")
            meta = self._fetch_public_track_page(track_id)
            return [meta] if meta else []

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """
        Return artist metadata + full album/single/EP list (no track listings).
        Used by the desktop UI discography picker — tracks are fetched later per
        selected album via the normal download pipeline.
        """
        import time as _time
        artist_id = _strip_id(url_or_id, "artist")
        if not self.sp:
            logger.info("[Spotify] No Spotify auth — falling back to public iTunes Search API for artist discography")
            return self._fetch_artist_discography_info_itunes(url_or_id)

        artist_data = self.sp.artist(artist_id)
        artist_name = artist_data.get("name", "Unknown Artist")
        images = artist_data.get("images", [])
        artwork_url = images[0]["url"] if images else None

        albums: list[dict] = []
        offset = 0
        limit = 50
        while True:
            resp = self.sp.artist_albums(
                artist_id,
                album_type="album,single,compilation",
                limit=limit,
                offset=offset,
                market=self._market or "US",
            )
            items = resp.get("items", [])
            if not items:
                break
            for item in items:
                release_date = item.get("release_date", "")
                year = int(release_date[:4]) if release_date and release_date[:4].isdigit() else None
                imgs = item.get("images", [])
                albums.append({
                    "id": item["id"],
                    "url": f"https://open.spotify.com/album/{item['id']}",
                    "name": item["name"],
                    "type": item.get("album_type", "album"),
                    "year": year,
                    "track_count": item.get("total_tracks", 0),
                    "artwork_url": imgs[0]["url"] if imgs else None,
                })
            if not resp.get("next"):
                break
            offset += limit
            _time.sleep(0.05)

        logger.info(f"Discography fetched for {artist_name}: {len(albums)} releases")
        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artwork_url": artwork_url,
            "albums": albums,
        }

    def _fetch_artist_discography_info_itunes(self, url_or_id: str) -> dict:
        """Fallback for fetching artist discography securely via iTunes Search API."""
        import json
        import requests
        from antra.utils.matching import normalize, string_similarity

        artist_id = _strip_id(url_or_id, "artist")
        fallback_name = "Unknown Artist"
        artwork_url = None

        # 1. Grab true artist name from Spotify embed page
        try:
            embed_url = f"https://open.spotify.com/embed/artist/{artist_id}"
            html = requests.get(embed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
            if match:
                data = json.loads(match.group(1))
                entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                fallback_name = entity.get("title") or entity.get("name") or fallback_name
                # Extract avatar
                visuals = entity.get("visuals", {})
                avatar = visuals.get("avatarImage", {}).get("sources", [])
                if avatar:
                    artwork_url = avatar[0].get("url")
        except Exception as e:
            logger.debug(f"[Spotify] Could not fetch artist name from embed: {e}")

        # 2. Search iTunes for the artist to get discography
        try:
            r = requests.get("https://itunes.apple.com/search", params={"term": fallback_name, "entity": "musicArtist", "limit": 10}, timeout=10)
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                raise RuntimeError(f"Could not find artist {fallback_name} on iTunes fallback search.")

            best = max(results, key=lambda a: string_similarity(normalize(fallback_name), normalize(a.get("artistName", ""))))
            itunes_id = best.get("artistId")
            artist_name = best.get("artistName", fallback_name)

            r2 = requests.get("https://itunes.apple.com/lookup", params={"id": itunes_id, "entity": "album", "limit": 200}, timeout=10)
            r2.raise_for_status()
            albums_data = r2.json().get("results", [])[1:]  # 0 is the artist entity itself

            albums = []
            for a in albums_data:
                name = a.get("collectionName", "")
                release_date = a.get("releaseDate", "")
                year = int(release_date[:4]) if release_date[:4].isdigit() else None
                a_url = a.get("collectionViewUrl", "")
                a_url = a_url.split("?")[0] if "?" in a_url else a_url
                img = a.get("artworkUrl100", "").replace("100x100bb", "600x600bb")

                albums.append({
                    "id": str(a.get("collectionId", "")),
                    "url": a_url,
                    "name": name,
                    "type": "album",
                    "year": year,
                    "track_count": a.get("trackCount", 0),
                    "artwork_url": img or None,
                })

            return {
                "artist_id": artist_id,
                "artist_name": artist_name,
                "artwork_url": artwork_url,
                "albums": albums,
            }
        except Exception as e:
            raise RuntimeError(f"[Spotify] iTunes fallback failed: {e}")

    def _fetch_artist_top(self, url_or_id: str) -> list[TrackMetadata]:
        artist_id = _strip_id(url_or_id, "artist")
        if not self.sp:
            return self._fetch_public_artist_page(artist_id)
            
        try:
            results = self.sp.artist_top_tracks(artist_id, country=self._market or "US")
            tracks = []
            for t in results.get("tracks", []):
                meta = self._parse_track(t)
                if meta:
                    tracks.append(meta)
            logger.info(f"Total top tracks for artist: {len(tracks)}")
            return tracks
        except Exception as e:
            logger.warning(f"Spotify artist API unavailable or credentials invalid for {artist_id}: {e}")
            return self._fetch_public_artist_page(artist_id)

    def _fetch_public_artist_page(self, artist_id: str) -> list[TrackMetadata]:
        """Scrape the artist embed page for their top tracks payload."""
        url = f"https://open.spotify.com/embed/artist/{artist_id}"
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            data = self._extract_next_data(response.text)
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            
            artist_name = entity.get("title") or entity.get("name") or "Unknown Artist"
            track_items = entity.get("trackList", [])
            
            tracks: list[TrackMetadata] = []
            for index, item in enumerate(track_items, start=1):
                track_uri = item.get("uri", "")
                if not track_uri.startswith("spotify:track:"):
                    continue
                title = item.get("title") or item.get("name") or "Unknown Track"
                artists = self._parse_public_artists(item.get("subtitle")) or [artist_name]
                duration_ms = item.get("duration") or item.get("durationMs")
                
                tracks.append(
                    TrackMetadata(
                        title=title,
                        artists=artists,
                        album="Unknown Album", # Top tracks usually don't have album contexts in embed payload
                        track_number=index,
                        duration_ms=duration_ms,
                        spotify_id=track_uri.split(":")[-1]
                    )
                )

            logger.info(f"Total top tracks in public artist fallback: {len(tracks)}")
            return tracks
        except Exception as e:
            logger.warning(f"Public artist fallback failed for {artist_id}: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────────────────────────────────

    def enrich_album_data(self, track: TrackMetadata) -> TrackMetadata:
        """Fetch album details (genres, full artwork URL) for a track."""
        if not track.album_id:
            return self.enrich_public_track_metadata(track)
        clients = [self.sp]
        if self._oauth_sp and self._oauth_sp is not self.sp:
            clients.append(self._oauth_sp)

        last_error = None
        for client in clients:
            try:
                album = client.album(track.album_id, market=self._market)
                genres = album.get("genres", [])
                if not genres:
                    artist_ids = [a["id"] for a in album.get("artists", []) if a.get("id")]
                    if artist_ids:
                        artist = client.artist(artist_ids[0])
                        genres = artist.get("genres", [])
                track.genres = genres

                images = album.get("images", [])
                if images:
                    track.artwork_url = images[0]["url"]

                track.total_tracks = album.get("total_tracks")
                return track
            except Exception as e:
                last_error = e

        if last_error:
            logger.warning(f"Failed to enrich album data for {track.title}: {last_error}")
        return self.enrich_public_track_metadata(track)

    def batch_enrich_album_data(self, tracks: list[TrackMetadata]) -> list[TrackMetadata]:
        """Enrich album data for a list of tracks using batched /albums API calls.

        Uses the Spotify /albums endpoint (up to 20 IDs per call) instead of one
        call per track, reducing API calls from N to ceil(unique_albums / 20).
        Falls back to enrich_public_track_metadata for any albums that fail.
        """
        # Build album_id → list of track indices mapping
        album_index: dict[str, list[int]] = {}
        for i, track in enumerate(tracks):
            if track.album_id:
                album_index.setdefault(track.album_id, []).append(i)

        unique_ids = list(album_index.keys())
        if not unique_ids:
            return tracks

        # Fetch in batches of 20 (Spotify API limit)
        album_data: dict[str, dict] = {}
        clients = [self.sp] if self.sp else []
        if self._oauth_sp and self._oauth_sp is not self.sp:
            clients.append(self._oauth_sp)
        if not clients:
            return tracks

        chunk_size = 20
        for start in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[start:start + chunk_size]
            for client in clients:
                try:
                    result = client.albums(chunk, market=self._market)
                    for album in (result.get("albums") or []):
                        if album and album.get("id"):
                            album_data[album["id"]] = album
                    break
                except Exception as e:
                    logger.warning(f"[Batch enrich] Failed to fetch album chunk: {e}")

        # Apply enriched data back to tracks
        for album_id, indices in album_index.items():
            album = album_data.get(album_id)
            if not album:
                continue
            genres = album.get("genres", [])
            if not genres:
                artist_ids = [a["id"] for a in album.get("artists", []) if a.get("id")]
                if artist_ids:
                    for client in clients:
                        try:
                            artist = client.artist(artist_ids[0])
                            genres = artist.get("genres", [])
                            break
                        except Exception:
                            pass
            images = album.get("images", [])
            artwork = images[0]["url"] if images else None
            total_tracks = album.get("total_tracks")
            for i in indices:
                if genres:
                    tracks[i].genres = genres
                if artwork and not tracks[i].artwork_url:
                    tracks[i].artwork_url = artwork
                if total_tracks:
                    tracks[i].total_tracks = total_tracks

        # Fall back for any tracks whose album wasn't fetched
        for i, track in enumerate(tracks):
            if track.album_id and track.album_id not in album_data:
                tracks[i] = self.enrich_public_track_metadata(track)

        return tracks

    def enrich_public_track_metadata(self, track: TrackMetadata) -> TrackMetadata:
        """Best-effort artwork fallback using Spotify's public track page."""
        if not track.spotify_id:
            return track

        try:
            public_data = self._fetch_public_track_page_data(track.spotify_id)
            artwork_url = public_data.get("artwork_url")
            album = public_data.get("album")
            artists = public_data.get("artists") or []

            if not track.artwork_url and artwork_url:
                track.artwork_url = artwork_url
            if (not track.album or track.album == "Unknown Album") and album:
                track.album = album
            if not track.artists and artists:
                track.artists = artists
        except Exception as e:
            logger.debug(f"Public track artwork fallback failed for {track.spotify_id}: {e}")
        return track

    def _fetch_public_track_page(self, track_id: str) -> Optional[TrackMetadata]:
        public_data = self._fetch_public_track_page_data(track_id)
        title = public_data.get("title")
        if not title:
            return None

        return TrackMetadata(
            title=title,
            artists=public_data.get("artists") or ["Unknown Artist"],
            album=public_data.get("album") or "Unknown Album",
            duration_ms=public_data.get("duration_ms"),
            spotify_id=track_id,
            artwork_url=public_data.get("artwork_url"),
        )

    def _fetch_public_track_page_data(self, track_id: str) -> dict:
        url = f"https://open.spotify.com/track/{track_id}"
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        html = response.text

        title = self._extract_meta_content(html, "og:title")
        description = self._extract_meta_content(html, "og:description")
        artwork_url = self._extract_meta_content(html, "og:image")
        duration_ms = self._extract_public_track_duration_ms(html)
        parsed_description = self._parse_public_track_description(description)
        album = parsed_description.get("album") or "Unknown Album"

        try:
            data = self._extract_next_data(html)
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            title = entity.get("title") or entity.get("name") or title
            description = entity.get("subtitle") or description
            album = entity.get("context", {}).get("name") or entity.get("album", {}).get("name") or album
            duration_ms = (
                entity.get("duration")
                or entity.get("durationMs")
                or duration_ms
            )
            image = entity.get("image") or entity.get("imageUrl")
            if isinstance(image, str) and image:
                artwork_url = image
            elif isinstance(image, list) and image:
                first = image[0]
                if isinstance(first, dict) and first.get("url"):
                    artwork_url = first["url"]
        except Exception:
            pass

        artists = parsed_description.get("artists") or self._parse_public_artists(description)
        return {
            "title": title,
            "artists": artists,
            "album": album or "Unknown Album",
            "artwork_url": artwork_url,
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _extract_meta_content(html: str, property_name: str) -> Optional[str]:
        import html as _html_module
        match = re.search(
            rf'<meta\s+(?:property|name)="{re.escape(property_name)}"\s+content="([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if match:
            return _html_module.unescape(match.group(1))
        return None

    @classmethod
    def _extract_public_track_duration_ms(cls, html: str) -> Optional[int]:
        candidates = (
            cls._extract_meta_content(html, "music:duration"),
            cls._extract_meta_content(html, "twitter:audio:duration"),
        )
        for raw_value in candidates:
            if not raw_value:
                continue
            try:
                return int(float(raw_value) * 1000)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _parse_public_artists(description: Optional[str]) -> list[str]:
        if not description:
            return []
        parts = [part.strip() for part in re.split(r"[·,]", description) if part.strip()]
        if len(parts) <= 1:
            return parts
        return parts[:2]

    @classmethod
    def _parse_public_track_description(cls, description: Optional[str]) -> dict:
        if not description:
            return {"artists": [], "album": None}
        dot_parts = [part.strip() for part in description.split("·") if part.strip()]
        if len(dot_parts) >= 3 and dot_parts[2].lower() == "song":
            return {
                "artists": cls._parse_public_artists(dot_parts[0]),
                "album": dot_parts[1] or None,
            }
        return {
            "artists": cls._parse_public_artists(description),
            "album": None,
        }

    def _parse_track(self, track: dict) -> Optional[TrackMetadata]:
        try:
            artists = [a["name"] for a in track.get("artists", [])]
            album_data = track.get("album", {})
            release_date = album_data.get("release_date", "")
            release_year = int(release_date[:4]) if release_date else None
            images = album_data.get("images", [])
            artwork_url = images[0]["url"] if images else None
            album_artists = [a["name"] for a in album_data.get("artists", []) if a.get("name")]

            return TrackMetadata(
                title=track["name"],
                artists=artists,
                album=album_data.get("name", "Unknown Album"),
                album_artists=album_artists,
                release_year=release_year,
                release_date=release_date,
                track_number=track.get("track_number"),
                disc_number=track.get("disc_number"),
                duration_ms=track.get("duration_ms"),
                isrc=track.get("external_ids", {}).get("isrc"),
                spotify_id=track.get("id"),
                album_id=album_data.get("id"),
                artwork_url=artwork_url,
            )
        except Exception as e:
            logger.warning(f"Failed to parse track: {e} — raw: {track.get('name')}")
            return None
