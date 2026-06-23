"""
Spotify Library client — Liked Songs + user playlists (including algorithmic).

Uses the sp_dc cookie to get a bearer token via the TOTP flow (reason=transport,
with server-time clock-skew protection), then uses the GraphQL Partner API
(api-partner.spotify.com) — the same backend as the Spotify web player.

CRITICAL: As of 2026 the REST endpoints are dead for sp_dc tokens:
  - /v1/me/playlists was REMOVED by Spotify in February 2026
  - /v1/me/tracks permanently 429-blocks sp_dc tokens
The GraphQL Partner API is the only reliable path.

Approach mirrors the Stash Android client (SpotifyAuthManager.kt + SpotifyApiClient.kt).
"""
import json as _json
import logging
import re
import threading
import time
import uuid
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────
_TOKEN_URL = "https://open.spotify.com/api/token"
_CLIENTTOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
_GRAPHQL_URL = "https://api-partner.spotify.com/pathfinder/v1/query"
_OPEN_SPOTIFY = "https://open.spotify.com/"

# ── TOTP ──────────────────────────────────────────────────────────────────────
# Same secret used in spotify.py — confirmed matching SpotiFLAC v61.
_SP_TOTP_SECRET = (
    "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4D"
    "QOJUGQYTAMRRGA2TCMJSHE3TCMBY"
)
_SP_TOTP_VERSION = 61

# ── GraphQL persisted-query hashes (from Stash SpotifyAuthConfig.kt) ─────────
_HASH_LIBRARY_V3 = (
    "973e511ca44261fda7eebac8b653155e7caee3675abb4fb110cc1b8c78b091c3"
)
_HASH_FETCH_LIBRARY_TRACKS = (
    "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240"
)

# ── HTTP defaults ─────────────────────────────────────────────────────────────
_CLIENT_VERSION_FALLBACK = "1.2.87.311.g2db0c2c4"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# Algorithmic playlist name patterns — used to flag them in the UI
_ALGO_NAMES = re.compile(
    r"daily mix|discover weekly|release radar|on repeat|repeat rewind"
    r"|daily drive|made for you|radar|mix \d|mix\d|weekly discovery"
    r"|time capsule|daylist",
    re.IGNORECASE,
)


class SpotifyLibraryClient:
    """Fetch a user's Spotify library (playlists + Liked Songs count).

    Requires a valid ``sp_dc`` session cookie from open.spotify.com.
    Uses the GraphQL Partner API via two-token flow (access token + client token).
    """

    def __init__(self, sp_dc: str):
        if not sp_dc:
            raise ValueError("sp_dc cookie is required for Spotify Library access.")
        self._sp_dc = sp_dc
        self._lock = threading.Lock()

        # Access token state
        self._access_token: Optional[str] = None
        self._access_token_expires: float = 0.0
        self._client_id: Optional[str] = None

        # Client token (required by GraphQL Partner API)
        self._client_token: Optional[str] = None

        # sp_t device ID — captured from token Set-Cookie; used in client token request
        self._sp_t: Optional[str] = None

    # ── Server time (clock-skew safe TOTP) ───────────────────────────────────

    def _get_server_time(self) -> Optional[int]:
        """Fetch Spotify's server Unix timestamp from the HTTP Date header."""
        try:
            r = requests.head(
                _OPEN_SPOTIFY,
                headers={"User-Agent": _UA},
                timeout=8,
                allow_redirects=True,
            )
            date_header = r.headers.get("Date")
            if date_header:
                from email.utils import parsedate_to_datetime
                return int(parsedate_to_datetime(date_header).timestamp())
        except Exception:
            pass
        return None

    # ── Access token ──────────────────────────────────────────────────────────

    def _fetch_access_token(self) -> tuple[str, float, str]:
        """Exchange sp_dc → short-lived bearer token via TOTP flow.

        Returns (access_token, expires_at_epoch, client_id).
        Raises RuntimeError when sp_dc is invalid/expired.
        """
        try:
            import pyotp
        except ImportError:
            raise RuntimeError("pyotp is required. Run: pip install pyotp")

        server_time = self._get_server_time() or int(time.time())
        totp = pyotp.TOTP(_SP_TOTP_SECRET)
        code = totp.at(for_time=server_time)

        url = (
            f"{_TOKEN_URL}"
            f"?reason=transport"
            f"&productType=web-player"
            f"&totp={code}"
            f"&totpServer={code}"
            f"&totpVer={_SP_TOTP_VERSION}"
        )
        r = requests.get(
            url,
            headers={
                "User-Agent": _UA,
                "Cookie": f"sp_dc={self._sp_dc}",
                "Accept": "application/json",
                "App-Platform": "WebPlayer",
                "Referer": _OPEN_SPOTIFY,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        if data.get("isAnonymous", True):
            raise RuntimeError(
                "sp_dc cookie is invalid or expired. "
                "Please update it in Settings → Spotify Library."
            )

        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError(f"No access token in Spotify response: {list(data)}")

        exp_ms = data.get("accessTokenExpirationTimestampMs", 0)
        expires = exp_ms / 1000 if exp_ms else time.time() + 3600
        client_id = data.get("clientId", "")

        # Capture sp_t device ID from Set-Cookie (used for client token requests).
        # requests stores multiple Set-Cookie values in the raw response.
        try:
            raw_cookies = r.raw.headers.getlist("Set-Cookie") if hasattr(r.raw.headers, "getlist") else []
            for sc in raw_cookies:
                if sc.startswith("sp_t="):
                    val = sc.split("=", 1)[1].split(";", 1)[0]
                    if val:
                        self._sp_t = val
                        break
        except Exception:
            pass

        return token, expires, client_id

    def _ensure_access_token(self) -> str:
        with self._lock:
            if self._access_token and time.time() < self._access_token_expires - 120:
                return self._access_token
            token, expires, client_id = self._fetch_access_token()
            self._access_token = token
            self._access_token_expires = expires
            self._client_id = client_id
            self._client_token = None  # invalidate when access token rotates
            return token

    # ── Client token ──────────────────────────────────────────────────────────

    def _get_client_version(self) -> str:
        """Scrape the Spotify web player client version for client token requests."""
        try:
            r = requests.get(
                _OPEN_SPOTIFY,
                headers={"User-Agent": _UA},
                timeout=10,
            )
            m = re.search(r'"clientVersion"\s*:\s*"([^"]+)"', r.text)
            if m:
                return m.group(1)
        except Exception:
            pass
        return _CLIENT_VERSION_FALLBACK

    def _fetch_client_token(self) -> str:
        """POST to clienttoken.spotify.com to get a Partner API client token."""
        client_id = self._client_id or ""
        client_version = self._get_client_version()
        device_id = self._sp_t or str(uuid.uuid4())

        payload = {
            "client_data": {
                "client_version": client_version,
                "client_id": client_id,
                "js_sdk_data": {
                    "device_brand": "unknown",
                    "device_model": "unknown",
                    "os": "windows",
                    "os_version": "NT 10.0",
                    "device_id": device_id,
                    "device_type": "computer",
                },
            }
        }

        # Try curl_cffi first (Chrome TLS fingerprint avoids Spotify JA3 fingerprinting)
        # Fall back to requests if curl_cffi is unavailable
        response_data = None
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.post(
                _CLIENTTOKEN_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _UA,
                },
                impersonate="chrome132",
                timeout=15,
            )
            resp.raise_for_status()
            response_data = resp.json()
        except ImportError:
            pass
        except Exception as e:
            logger.debug("[SpotifyLibrary] curl_cffi client token failed: %s", e)

        if response_data is None:
            resp = requests.post(
                _CLIENTTOKEN_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _UA,
                },
                timeout=15,
            )
            resp.raise_for_status()
            response_data = resp.json()

        if response_data.get("response_type") == "RESPONSE_GRANTED_TOKEN_RESPONSE":
            token = (response_data.get("granted_token") or {}).get("token")
            if token:
                return token

        raise RuntimeError(
            f"Client token request failed (response_type: {response_data.get('response_type')})"
        )

    def _ensure_client_token(self) -> str:
        if self._client_token:
            return self._client_token
        self._client_token = self._fetch_client_token()
        return self._client_token

    # ── GraphQL Partner API ───────────────────────────────────────────────────

    def _graphql(self, operation: str, variables: dict, hash_: str) -> Optional[dict]:
        """Execute a persisted GraphQL query against api-partner.spotify.com."""
        import urllib.parse

        access_token = self._ensure_access_token()
        client_token = self._ensure_client_token()

        encoded_vars = urllib.parse.quote(_json.dumps(variables, separators=(",", ":")))
        extensions = _json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": hash_}},
            separators=(",", ":"),
        )
        encoded_ext = urllib.parse.quote(extensions)

        url = (
            f"{_GRAPHQL_URL}"
            f"?operationName={operation}"
            f"&variables={encoded_vars}"
            f"&extensions={encoded_ext}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Token": client_token,
            "Accept": "application/json",
            "App-Platform": "WebPlayer",
            "Spotify-App-Version": _CLIENT_VERSION_FALLBACK,
            "Origin": "https://open.spotify.com",
            "Referer": _OPEN_SPOTIFY,
            "User-Agent": _UA,
        }

        r = requests.get(url, headers=headers, timeout=20)

        if r.status_code == 401:
            # Token expired mid-session; force refresh once
            with self._lock:
                self._access_token = None
                self._client_token = None
            access_token = self._ensure_access_token()
            client_token = self._ensure_client_token()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["Client-Token"] = client_token
            r = requests.get(url, headers=headers, timeout=20)

        r.raise_for_status()
        return r.json()

    # ── Liked Songs count ─────────────────────────────────────────────────────

    def _get_liked_songs_count(self) -> int:
        """Get total Liked Songs count via fetchLibraryTracks GraphQL."""
        try:
            data = self._graphql(
                "fetchLibraryTracks",
                {"offset": 0, "limit": 1},
                _HASH_FETCH_LIBRARY_TRACKS,
            )
            if not data:
                return 0
            me = (data.get("data") or {}).get("me") or {}
            # Try both known response paths
            library = me.get("library") or {}
            tracks = library.get("tracks") or me.get("libraryTracks") or {}
            total = tracks.get("totalCount")
            if total is not None:
                return int(total)
            # If totalCount absent, count items on this page as lower bound
            items = tracks.get("items") or []
            return len(items)
        except Exception as e:
            logger.warning("[SpotifyLibrary] fetchLibraryTracks failed: %s", e)
            return 0

    # ── Playlists via libraryV3 ───────────────────────────────────────────────

    _count_keys_logged = False

    @staticmethod
    def _extract_count(*objs) -> int:
        """Find a track/item count across the several shapes Spotify's libraryV3
        returns it in. Different persisted-query hash versions nest it differently:
          data.content.totalCount, data.contentRoot.totalCount, item.count,
          data.attributes (totalCount), data.totalItems / numberOfTracks, etc.
        Returns the first positive integer found, else 0."""
        def _dig(o):
            if not isinstance(o, dict):
                return 0
            for key in ("totalCount", "totalItems", "trackCount", "numberOfTracks",
                        "numTracks", "count"):
                v = o.get(key)
                if isinstance(v, int) and v > 0:
                    return v
                if isinstance(v, str) and v.isdigit() and int(v) > 0:
                    return int(v)
            for sub in ("content", "contentRoot", "items", "tracks", "attributes"):
                v = _dig(o.get(sub))
                if v:
                    return v
            return 0

        for obj in objs:
            n = _dig(obj)
            if n:
                return n
        return 0

    def _get_all_playlists(self) -> list:
        """Fetch all library playlists using libraryV3 GraphQL.

        Descends into folders (libraryV3 is hierarchical — playlists inside
        a folder are only visible when re-queried with that folderUri).
        Uses rawItemCount for pagination to avoid early termination when a
        page contains folder or non-playlist items.
        """
        all_playlists: list = []
        seen_ids: set = set()

        # BFS queue: None = root level, string = folder URI to descend into
        folders_to_visit: list = [None]

        while folders_to_visit:
            folder_uri = folders_to_visit.pop(0)
            offset = 0
            limit = 50

            while True:
                variables: dict = {
                    "filters": ["Playlists"],
                    "order": None,
                    "textFilter": "",
                    "features": ["LIKED_SONGS", "YOUR_EPISODES"],
                    "limit": limit,
                    "offset": offset,
                }
                if folder_uri is not None:
                    variables["folderUri"] = folder_uri

                try:
                    data = self._graphql("libraryV3", variables, _HASH_LIBRARY_V3)
                except Exception as e:
                    logger.warning(
                        "[SpotifyLibrary] libraryV3 failed (folder=%s offset=%d): %s",
                        folder_uri, offset, e,
                    )
                    break

                items = (
                    (data or {})
                    .get("data", {})
                    .get("me", {})
                    .get("libraryV3", {})
                    .get("items") or []
                )

                raw_count = len(items)  # pagination must use this, not parsed count

                for element in items:
                    try:
                        item = (element.get("item") or {})
                        data_obj = item.get("data") or {}
                        type_name = data_obj.get("__typename", "")
                        uri = data_obj.get("uri", "")

                        # Folder: queue for descent
                        if type_name == "Folder" or ":folder:" in uri:
                            if uri:
                                folders_to_visit.append(uri)
                            continue

                        if type_name != "Playlist":
                            continue
                        if not uri.startswith("spotify:playlist:"):
                            continue

                        playlist_id = uri[len("spotify:playlist:"):]
                        if not playlist_id or playlist_id in seen_ids:
                            continue
                        seen_ids.add(playlist_id)

                        name = (data_obj.get("name") or "").strip()
                        if not name:
                            continue

                        owner = (data_obj.get("ownerV2") or {}).get("data") or {}
                        owner_id = (owner.get("username") or "").strip()

                        is_algo = owner_id == "spotify" or bool(_ALGO_NAMES.search(name))

                        # Image URL — structure: images.items[0].sources[0].url
                        images_obj = data_obj.get("images") or {}
                        image_items = images_obj.get("items") if isinstance(images_obj, dict) else []
                        image_url: Optional[str] = None
                        if image_items:
                            first = image_items[0] if image_items else {}
                            if isinstance(first, dict):
                                sources = first.get("sources") or []
                                if sources and isinstance(sources[0], dict):
                                    image_url = sources[0].get("url")

                        track_count = self._extract_count(data_obj, element)
                        if not track_count and not SpotifyLibraryClient._count_keys_logged:
                            SpotifyLibraryClient._count_keys_logged = True
                            logger.debug(
                                "[SpotifyLibrary] playlist count not found; data keys=%s element keys=%s",
                                list(data_obj.keys()), list(element.keys()),
                            )
                        description = (data_obj.get("description") or "").strip()

                        all_playlists.append({
                            "id": playlist_id,
                            "name": name,
                            "url": f"https://open.spotify.com/playlist/{playlist_id}",
                            "image_url": image_url,
                            "track_count": track_count,
                            "owner_id": owner_id,
                            "is_algorithmic": is_algo,
                            "description": description,
                        })
                    except Exception as item_err:
                        logger.debug("[SpotifyLibrary] skipping item: %s", item_err)
                        continue

                if raw_count < limit:
                    break
                offset += limit

        # Algorithmic playlists first, then personal alphabetically
        all_playlists.sort(
            key=lambda p: (0 if p["is_algorithmic"] else 1, p["name"].lower())
        )
        return all_playlists

    # ── Saved albums via libraryV3 ────────────────────────────────────────────

    def _get_saved_albums(self) -> list:
        """Fetch all saved albums using libraryV3 GraphQL (filters=["Albums"])."""
        albums: list = []
        seen_ids: set = set()
        offset = 0
        limit = 50

        while True:
            variables = {
                "filters": ["Albums"],
                "order": None,
                "textFilter": "",
                "features": [],
                "limit": limit,
                "offset": offset,
            }
            try:
                data = self._graphql("libraryV3", variables, _HASH_LIBRARY_V3)
            except Exception as e:
                logger.warning("[SpotifyLibrary] libraryV3 albums failed (offset=%d): %s", offset, e)
                break

            items = (
                (data or {})
                .get("data", {})
                .get("me", {})
                .get("libraryV3", {})
                .get("items") or []
            )
            raw_count = len(items)

            for element in items:
                try:
                    item = element.get("item") or {}
                    data_obj = item.get("data") or {}
                    type_name = data_obj.get("__typename", "")
                    uri = data_obj.get("uri", "")

                    if type_name != "Album" or not uri.startswith("spotify:album:"):
                        continue

                    album_id = uri[len("spotify:album:"):]
                    if not album_id or album_id in seen_ids:
                        continue
                    seen_ids.add(album_id)

                    name = (data_obj.get("name") or "").strip()
                    if not name:
                        continue

                    # Artists — albums use artists.items[].profile.name
                    artist_names = []
                    artists_obj = data_obj.get("artists") or {}
                    for a in (artists_obj.get("items") or []):
                        prof = (a.get("profile") or a)
                        nm = prof.get("name")
                        if nm:
                            artist_names.append(nm)
                    artist_str = ", ".join(artist_names)

                    # Cover art — coverArt.sources[].url
                    image_url: Optional[str] = None
                    cover = data_obj.get("coverArt") or {}
                    for src in (cover.get("sources") or []):
                        if isinstance(src, dict) and src.get("url"):
                            image_url = src["url"]
                            break

                    # Release year
                    year = None
                    date_obj = data_obj.get("date") or {}
                    if isinstance(date_obj, dict):
                        year = date_obj.get("year")

                    albums.append({
                        "id": album_id,
                        "name": name,
                        "url": f"https://open.spotify.com/album/{album_id}",
                        "image_url": image_url,
                        "artists": artist_str,
                        "year": year,
                    })
                except Exception as item_err:
                    logger.debug("[SpotifyLibrary] skipping album item: %s", item_err)
                    continue

            if raw_count < limit:
                break
            offset += limit

        albums.sort(key=lambda a: a["name"].lower())
        return albums

    # ── Followed artists via libraryV3 ────────────────────────────────────────

    def _get_followed_artists(self) -> list:
        """Fetch all followed/saved artists using libraryV3 GraphQL (filters=["Artists"])."""
        artists: list = []
        seen_ids: set = set()
        offset = 0
        limit = 50

        while True:
            variables = {
                "filters": ["Artists"],
                "order": None,
                "textFilter": "",
                "features": [],
                "limit": limit,
                "offset": offset,
            }
            try:
                data = self._graphql("libraryV3", variables, _HASH_LIBRARY_V3)
            except Exception as e:
                logger.warning("[SpotifyLibrary] libraryV3 artists failed (offset=%d): %s", offset, e)
                break

            items = (
                (data or {})
                .get("data", {})
                .get("me", {})
                .get("libraryV3", {})
                .get("items") or []
            )
            raw_count = len(items)

            for element in items:
                try:
                    item = element.get("item") or {}
                    data_obj = item.get("data") or {}
                    type_name = data_obj.get("__typename", "")
                    uri = data_obj.get("uri", "")

                    if type_name != "Artist" or not uri.startswith("spotify:artist:"):
                        continue

                    artist_id = uri[len("spotify:artist:"):]
                    if not artist_id or artist_id in seen_ids:
                        continue
                    seen_ids.add(artist_id)

                    # Artist name — profile.name or top-level name
                    profile = data_obj.get("profile") or {}
                    name = (profile.get("name") or data_obj.get("name") or "").strip()
                    if not name:
                        continue

                    # Avatar image — visuals.avatarImage.sources[].url
                    image_url: Optional[str] = None
                    visuals = data_obj.get("visuals") or {}
                    avatar = visuals.get("avatarImage") or {}
                    for src in (avatar.get("sources") or []):
                        if isinstance(src, dict) and src.get("url"):
                            image_url = src["url"]
                            break

                    artists.append({
                        "id": artist_id,
                        "name": name,
                        "url": f"https://open.spotify.com/artist/{artist_id}",
                        "image_url": image_url,
                    })
                except Exception as item_err:
                    logger.debug("[SpotifyLibrary] skipping artist item: %s", item_err)
                    continue

            if raw_count < limit:
                break
            offset += limit

        artists.sort(key=lambda a: a["name"].lower())
        return artists

    # ── Liked Songs tracks ────────────────────────────────────────────────────

    def get_liked_songs_tracks(self, page_callback=None) -> list:
        """Fetch all Liked Songs as a list of dicts with Spotify track metadata.

        Uses the fetchLibraryTracks GraphQL query with pagination.
        Each item shape: {id, name, artists, album, album_id, duration_ms,
                          isrc, release_date, artwork_url, explicit}
        Calls page_callback(list_so_far) after each page (for progressive UI).
        """
        all_tracks: list = []
        offset = 0
        limit = 50

        while True:
            try:
                data = self._graphql(
                    "fetchLibraryTracks",
                    {"offset": offset, "limit": limit},
                    _HASH_FETCH_LIBRARY_TRACKS,
                )
            except Exception as e:
                logger.warning("[SpotifyLibrary] fetchLibraryTracks page failed (offset=%d): %s", offset, e)
                break

            if not data:
                break

            me = (data.get("data") or {}).get("me") or {}
            library = me.get("library") or {}
            tracks_obj = library.get("tracks") or me.get("libraryTracks") or {}
            items = tracks_obj.get("items") or []

            if not items:
                break

            for element in items:
                try:
                    # Partner API wraps entity data under .track.data (with _uri on wrapper)
                    # Try item/itemV2 as fallbacks (same shape, different response keys)
                    wrapper = (
                        element.get("track")
                        or element.get("item")
                        or element.get("itemV2")
                        or {}
                    )
                    track = wrapper.get("data") or wrapper  # descend into data if present
                    if not track:
                        continue

                    uri = track.get("uri") or wrapper.get("_uri") or ""
                    if uri and not uri.startswith("spotify:track:"):
                        continue  # skip podcasts, local files, etc.
                    track_id = track.get("id") or (uri.split(":")[-1] if uri else "")
                    if not track_id:
                        continue

                    name = track.get("name") or ""
                    artists_list = [
                        (a.get("profile") or a).get("name", "")
                        for a in (track.get("artists") or {}).get("items") or []
                    ]
                    album_obj = track.get("albumOfTrack") or track.get("album") or {}
                    album_name = album_obj.get("name") or ""
                    album_id = album_obj.get("id") or album_obj.get("uri", "").split(":")[-1]

                    # Artwork
                    artwork_url: Optional[str] = None
                    cover_art = album_obj.get("coverArt") or {}
                    for src in (cover_art.get("sources") or []):
                        if isinstance(src, dict) and src.get("url"):
                            artwork_url = src["url"]
                            break

                    # Duration — Partner API uses trackDuration or duration
                    dur = track.get("trackDuration") or track.get("duration") or {}
                    duration_ms = dur.get("totalMilliseconds") or 0

                    # ISRC / release date
                    isrc = None
                    release_date = None
                    if track.get("externalIds"):
                        isrc = (track["externalIds"] or {}).get("isrc")
                    album_date = album_obj.get("date") or {}
                    if isinstance(album_date, dict):
                        release_date = album_date.get("isoString") or str(album_date.get("year") or "")
                    elif isinstance(album_date, str):
                        release_date = album_date

                    content_rating = track.get("contentRating") or {}
                    explicit = (content_rating.get("label") == "EXPLICIT") if isinstance(content_rating, dict) else False

                    all_tracks.append({
                        "id": track_id,
                        "name": name,
                        "artists": artists_list,
                        "album": album_name,
                        "album_id": album_id,
                        "duration_ms": duration_ms,
                        "isrc": isrc,
                        "release_date": release_date,
                        "artwork_url": artwork_url,
                        "explicit": explicit,
                        "spotify_id": track_id,
                    })
                except Exception as item_err:
                    logger.debug("[SpotifyLibrary] skipping liked track item: %s", item_err)
                    continue

            if page_callback and all_tracks:
                try:
                    page_callback(list(all_tracks))
                except Exception:
                    pass

            if len(items) < limit:
                break
            offset += limit

        return all_tracks

    # ── Public API ───────────────────────────────────────────────────────────

    def get_library(self) -> dict:
        """Return the full library dict consumed by the Go binding.

        Shape::

            {
                "liked_songs_count": 1234,
                "playlists": [
                    {
                        "id": "37i9dQZEVXcSl5JcFboUlo",
                        "name": "Discover Weekly",
                        "url": "https://open.spotify.com/playlist/37i9dQZEVXcSl5JcFboUlo",
                        "image_url": "https://...",
                        "track_count": 30,
                        "owner_id": "spotify",
                        "is_algorithmic": true,
                        "description": "Your weekly mixtape..."
                    },
                    ...
                ],
                "saved_albums": [
                    {"id", "name", "url", "image_url", "artists", "year"},
                    ...
                ],
                "followed_artists": [
                    {"id", "name", "url", "image_url"},
                    ...
                ]
            }
        """
        liked_count = self._get_liked_songs_count()
        playlists = self._get_all_playlists()
        saved_albums = self._get_saved_albums()
        followed_artists = self._get_followed_artists()
        return {
            "liked_songs_count": liked_count,
            "playlists": playlists,
            "saved_albums": saved_albums,
            "followed_artists": followed_artists,
        }
