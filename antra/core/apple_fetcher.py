"""
Apple Music URL fetcher — extracts track lists from Apple Music URLs.

Supports:
  - Songs:     https://music.apple.com/us/album/{name}/{album_id}?i={track_id}
  - Albums:    https://music.apple.com/us/album/{name}/{album_id}
  - Playlists: https://music.apple.com/us/playlist/{name}/{playlist_id}

Uses the free iTunes Search/Lookup API (no auth needed) for songs and albums.
Uses the Apple Music Catalog API for playlists — requires an Apple Developer
token, but we generate a short-lived anonymous one from a known public key that
Apple embeds in the web player JS (same technique as apple-music-metadata libs).

No account, no subscription, no credentials required.
"""

import json
import logging
import re
import time
from typing import Optional

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

# ── Apple Music URL patterns ───────────────────────────────────────────────────
# Slug segment is optional — Apple Music search results return bare /album/{id} URLs
_RE_SONG     = re.compile(r"music\.apple\.com/([a-z]{2})/album/(?:[^/]+/)?(\d+)\?i=(\d+)")
_RE_SONG_DIRECT = re.compile(r"music\.apple\.com/([a-z]{2})/song/(?:[^/]+/)?(\d+)")
_RE_ALBUM    = re.compile(r"music\.apple\.com/([a-z]{2})/album/(?:[^/]+/)?(\d+)(?!\?i=)")
_RE_PLAYLIST = re.compile(r"music\.apple\.com/([a-z]{2})/playlist/[^/]+/(pl\.[a-zA-Z0-9-]+)")

# ── iTunes API ────────────────────────────────────────────────────────────────
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
_ITUNES_SEARCH = "https://itunes.apple.com/search"

# ── Apple Music Catalog API ───────────────────────────────────────────────────
_AM_CATALOG = "https://api.music.apple.com/v1/catalog/{storefront}"

REQUEST_TIMEOUT = 15
MAX_PLAYLIST_PAGES = 20   # safety cap at 500 tracks (25 per page)


def is_apple_music_url(url: str) -> bool:
    """Return True if the URL looks like an Apple Music URL."""
    return "music.apple.com" in url


class AppleFetcher:
    """
    Resolves Apple Music URLs to lists of TrackMetadata.

    Does not download audio — just collects metadata (title, artist, album,
    ISRC, duration) for the Antra waterfall to act on.
    """

    def __init__(self, developer_token: Optional[str] = None):
        self._dev_token = developer_token  # optional — only needed for playlists
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

    # ── Public entry points ───────────────────────────────────────────────────

    def parse_url(self, url: str) -> tuple[str, str]:
        """
        Parse an Apple Music URL and return (url_type, entity_id) without
        making any network calls. Useful for validation and routing.

        url_type is one of: "song", "album", "playlist"
        entity_id is the iTunes track/collection ID or playlist ID.

        Raises ValueError for unrecognised URLs.
        """
        url = url.strip()

        m = _RE_SONG.search(url)
        if m:
            return ("song", m.group(3))  # iTunes track ID from ?i=<id>

        m = _RE_SONG_DIRECT.search(url)
        if m:
            return ("song", m.group(2))  # iTunes track ID from /song/id

        m = _RE_ALBUM.search(url)
        if m:
            return ("album", m.group(2))  # iTunes collection ID

        m = _RE_PLAYLIST.search(url)
        if m:
            return ("playlist", m.group(2))  # e.g. pl.f4d106fed2bd41149...

        raise ValueError(
            f"[Apple] Not a recognised Apple Music URL: {url}\n"
            "Supported: song, album, or playlist links from music.apple.com"
        )

    def fetch(self, url: str) -> list[TrackMetadata]:
        """
        Resolve an Apple Music URL to a list of TrackMetadata objects.
        Raises ValueError if the URL isn't a recognised Apple Music URL.
        Raises RuntimeError if the API call fails.
        """
        url_type, entity_id = self.parse_url(url)  # raises ValueError if invalid

        # Determine storefront from URL (default "us")
        sf_match = re.search(r"music\.apple\.com/([a-z]{2})/", url)
        storefront = sf_match.group(1) if sf_match else "us"

        if url_type == "song":
            logger.info(f"[Apple] Detected song URL — track ID {entity_id}")
            track = self._fetch_song(entity_id, storefront)
            return [track] if track else []

        if url_type == "album":
            logger.info(f"[Apple] Detected album URL — album ID {entity_id}")
            return self._fetch_album(entity_id, storefront)

        # playlist — extract human-readable name from URL slug as fallback
        slug_match = re.search(r"/playlist/([^/]+)/", url)
        url_slug_name = slug_match.group(1).replace("-", " ").title() if slug_match else "Apple Music Playlist"
        logger.info(f"[Apple] Detected playlist URL — {entity_id}")
        return self._fetch_playlist(entity_id, storefront, url_slug_name=url_slug_name)

    # ── Song ──────────────────────────────────────────────────────────────────

    def _fetch_song(self, track_id: str, storefront: str = "us") -> Optional[TrackMetadata]:
        """Look up a single song."""
        token = self._get_developer_token()
        if token:
            try:
                resp = self._session.get(
                    f"{_AM_CATALOG.format(storefront=storefront)}/songs/{track_id}",
                    headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.ok:
                    data = resp.json().get("data", [])
                    if data:
                        logger.debug(f"[Apple] Song {track_id} fetched via Catalog API")
                        return self._catalog_item_to_metadata(data[0].get("attributes", {}))
            except Exception as e:
                logger.debug(f"[Apple] Catalog API song fetch failed: {e}")

        def _lookup(country: str) -> list:
            try:
                resp = self._session.get(
                    _ITUNES_LOOKUP,
                    params={"id": track_id, "entity": "song", "country": country},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                items = resp.json().get("results", [])
                return [i for i in items if i.get("wrapperType") == "track" and i.get("kind") == "song"]
            except Exception as e:
                raise RuntimeError(f"[Apple] iTunes lookup failed for track {track_id}: {e}") from e

        songs = _lookup(storefront)
        if not songs:
            for sf in [s for s in ["cn", "tw", "hk", "jp", "kr", "gb", "au"] if s != storefront]:
                songs = _lookup(sf)
                if songs:
                    logger.debug(f"[Apple] Track {track_id} not in '{storefront}' catalog — found via '{sf}'")
                    break

        if not songs:
            logger.warning(f"[Apple] No song found for track ID {track_id}")
            return None

        return self._item_to_metadata(songs[0])

    # ── Album ─────────────────────────────────────────────────────────────────

    def _fetch_album(self, album_id: str, storefront: str = "us") -> list[TrackMetadata]:
        """Fetch all tracks in an album."""
        token = self._get_developer_token()
        if token:
            try:
                resp = self._session.get(
                    f"{_AM_CATALOG.format(storefront=storefront)}/albums/{album_id}",
                    headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
                    params={"include": "tracks"},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.ok:
                    album_data = resp.json().get("data", [])
                    if album_data:
                        album_attrs = album_data[0].get("attributes", {})
                        upc = album_attrs.get("upc")
                        cat_album_name = album_attrs.get("name", "")
                        cat_release_raw = (album_attrs.get("releaseDate") or "")[:10]
                        cat_artist_name = (album_attrs.get("artistName") or "").strip()
                        cat_album_artists = [cat_artist_name] if cat_artist_name else []

                        track_relationships = album_data[0].get("relationships", {}).get("tracks", {}).get("data", [])
                        tracks = []
                        for t in track_relationships:
                            meta = self._catalog_item_to_metadata(t.get("attributes", {}))
                            if meta:
                                if upc and not meta.upc:
                                    meta.upc = upc
                                # Override per-track albumName with the parent album name so
                                # edition variants (e.g. "Shady Edition") use their own folder
                                # instead of the canonical album name from the track attribute.
                                if cat_album_name:
                                    meta.album = cat_album_name
                                if cat_release_raw:
                                    meta.release_date = cat_release_raw
                                    try:
                                        meta.release_year = int(cat_release_raw[:4])
                                    except ValueError:
                                        pass
                                if cat_album_artists and not meta.album_artists:
                                    meta.album_artists = cat_album_artists
                                tracks.append(meta)
                        if tracks:
                            logger.info(f"[Apple] Fetched {len(tracks)} tracks from album '{cat_album_name}' (via Catalog API)")
                            return tracks
            except Exception as e:
                logger.debug(f"[Apple] Catalog API album fetch failed: {e}")

        def _lookup(country: str) -> list:
            try:
                resp = self._session.get(
                    _ITUNES_LOOKUP,
                    params={"id": album_id, "entity": "song", "limit": 200, "country": country},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json().get("results", [])
            except Exception as e:
                raise RuntimeError(f"[Apple] iTunes album lookup failed for {album_id}: {e}") from e

        results = _lookup(storefront)

        # Region-exclusive albums (e.g. Chinese releases on a /us/ URL with ?l=zh-Hans-CN)
        # iTunes may return the album collection item but 0 song tracks for the wrong storefront.
        # Fall back through other storefronts if no songs were returned.
        def _has_songs(r: list) -> bool:
            return any(x.get("wrapperType") == "track" and x.get("kind") == "song" for x in r)

        if not _has_songs(results):
            fallback_storefronts = [sf for sf in ["cn", "tw", "hk", "jp", "kr", "gb", "au"] if sf != storefront]
            for sf in fallback_storefronts:
                fallback = _lookup(sf)
                if _has_songs(fallback):
                    results = fallback
                    logger.debug(f"[Apple] Album {album_id} not in '{storefront}' catalog — found via '{sf}'")
                    break

        # First result is the album itself; rest are songs
        album_item = next((r for r in results if r.get("wrapperType") == "collection"), None)
        album_art = None
        album_name = ""
        total_tracks = None
        release_date = ""

        album_artists: list[str] = []
        if album_item:
            album_art = self._upgrade_artwork_url(album_item.get("artworkUrl100", ""))
            album_name = album_item.get("collectionName", "")
            total_tracks = album_item.get("trackCount")
            release_date = (album_item.get("releaseDate") or "")[:10]
            # Album-level artist (e.g. "PARTYNEXTDOOR & Drake") — stored verbatim
            # so all tracks land in one folder regardless of per-track features.
            album_artist_name = album_item.get("artistName", "").strip()
            if album_artist_name:
                album_artists = [album_artist_name]

        songs = [r for r in results if r.get("wrapperType") == "track" and r.get("kind") == "song"]
        if not songs:
            logger.warning(f"[Apple] No tracks found for album ID {album_id}")
            return []

        tracks = []
        for song in songs:
            meta = self._item_to_metadata(song)
            if meta:
                # Enrich with album-level data when available
                if album_art and not meta.artwork_url:
                    meta.artwork_url = album_art
                if total_tracks and meta.total_tracks is None:
                    meta.total_tracks = total_tracks
                if release_date:
                    meta.release_date = release_date
                    try:
                        meta.release_year = int(release_date[:4])
                    except ValueError:
                        pass
                # Override track collection name with the parent album name so
                # anomalous Apple multi-disc segments (e.g. "Disc 29") fall under the parent folder.
                if album_name:
                    meta.album = album_name

                # Stamp album-level artists so all tracks share one folder
                if album_artists and not meta.album_artists:
                    meta.album_artists = album_artists
                tracks.append(meta)

        # Apple Music sometimes glitches and assigns arbitrary high disc numbers (like 29 or 39)
        # to tracks on standard albums, which splits the album in music players
        unique_discs = {t.disc_number for t in tracks if t.disc_number is not None}
        if unique_discs and max(unique_discs) > 10:
            logger.debug(f"[Apple] Wiping anomalously high disc numbers: {sorted(list(unique_discs))}")
            for t in tracks:
                t.disc_number = 1

        logger.info(f"[Apple] Fetched {len(tracks)} tracks from album '{album_name or album_id}'")
        return tracks

    # ── Playlist ──────────────────────────────────────────────────────────────

    def _fetch_playlist(self, playlist_id: str, storefront: str = "us", url_slug_name: str = "Apple Music Playlist") -> list[TrackMetadata]:
        """
        Fetch tracks from an Apple Music playlist via the Catalog API.

        Tries two strategies:
        1. Apple Music Catalog API (needs a developer token — we try to auto-fetch one)
           Works for both Apple-curated and public user-created (pl.u-) playlists.
        2. iTunes URL fallback via RSS feed (only works for Apple-curated playlists)
        """
        token = self._get_developer_token()

        if token:
            tracks = self._playlist_via_catalog_api(playlist_id, storefront, token)
            if tracks:
                return tracks
            logger.warning("[Apple] Catalog API returned no tracks — trying RSS fallback")

        # RSS fallback only works for Apple-curated playlists (pl. without the u- prefix).
        # User-created playlists (pl.u-) are not surfaced by the RSS/iTunes lookup endpoint.
        if not playlist_id.startswith("pl.u-"):
            tracks = self._playlist_via_rss(playlist_id, storefront)
            if tracks:
                for i, track in enumerate(tracks):
                    if not track.playlist_name:
                        track.playlist_name = url_slug_name
                        track.playlist_position = track.playlist_position or (i + 1)
                return tracks

        if playlist_id.startswith("pl.u-"):
            raise RuntimeError(
                f"[Apple] Could not fetch playlist {playlist_id}.\n"
                "Make sure this playlist is set to public/shareable in Apple Music. "
                "Private user-created playlists cannot be accessed without an Apple Music developer token."
            )

        raise RuntimeError(
            f"[Apple] Could not fetch playlist {playlist_id}.\n"
            "For user-created or private Apple Music playlists, set APPLE_DEVELOPER_TOKEN in your .env."
        )

    def _fetch_playlist_meta(self, playlist_id: str, storefront: str, token: str) -> tuple[str, Optional[str]]:
        """Return (playlist_name, playlist_artwork_url) from the Catalog API."""
        try:
            resp = self._session.get(
                f"{_AM_CATALOG.format(storefront=storefront)}/playlists/{playlist_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://music.apple.com",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.ok:
                data = resp.json().get("data", [])
                if data:
                    attrs = data[0].get("attributes", {})
                    name = attrs.get("name", "") or "Apple Music Playlist"
                    artwork = attrs.get("artwork", {})
                    artwork_url: Optional[str] = None
                    if artwork:
                        w = artwork.get("width") or 3000
                        h = artwork.get("height") or 3000
                        url_tpl = artwork.get("url", "")
                        if url_tpl:
                            artwork_url = url_tpl.replace("{w}", str(w)).replace("{h}", str(h))
                    return name, artwork_url
        except Exception as e:
            logger.debug(f"[Apple] Could not fetch playlist meta: {e}")
        return "Apple Music Playlist", None

    def _fetch_playlist_name(self, playlist_id: str, storefront: str, token: str) -> str:
        name, _ = self._fetch_playlist_meta(playlist_id, storefront, token)
        return name

    def _playlist_via_catalog_api(
        self,
        playlist_id: str,
        storefront: str,
        token: str,
    ) -> list[TrackMetadata]:
        """Paginate through the Apple Music Catalog API to fetch all playlist tracks."""
        playlist_name, playlist_artwork = self._fetch_playlist_meta(playlist_id, storefront, token)

        base_url = f"{_AM_CATALOG.format(storefront=storefront)}/playlists/{playlist_id}/tracks"
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://music.apple.com",
        }

        tracks: list[TrackMetadata] = []
        url: Optional[str] = base_url
        params: dict = {"limit": 25, "include": "catalog"}
        page = 0

        while url and page < MAX_PLAYLIST_PAGES:
            try:
                resp = self._session.get(
                    url,
                    headers=headers,
                    params=params if page == 0 else None,
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as e:
                logger.warning(f"[Apple] Catalog API request failed: {e}")
                break

            if resp.status_code == 401:
                logger.warning("[Apple] Catalog API: 401 Unauthorized — developer token rejected")
                break
            if resp.status_code == 404:
                logger.warning(f"[Apple] Catalog API: playlist {playlist_id} not found")
                break
            if not resp.ok:
                logger.warning(f"[Apple] Catalog API returned {resp.status_code}")
                break

            data = resp.json()
            items = data.get("data", [])

            for item in items:
                attrs = item.get("attributes", {})
                meta = self._catalog_item_to_metadata(attrs)
                if meta:
                    meta.playlist_name = playlist_name
                    meta.playlist_position = len(tracks) + 1
                    if playlist_artwork:
                        meta.playlist_artwork_url = playlist_artwork
                    tracks.append(meta)

            # Pagination
            next_url = data.get("next")
            url = f"https://api.music.apple.com{next_url}" if next_url else None
            page += 1
            if url:
                time.sleep(0.2)  # polite pacing

        logger.info(f"[Apple] Fetched {len(tracks)} tracks from playlist '{playlist_name}' via Catalog API")
        return tracks

    def _playlist_via_rss(self, playlist_id: str, storefront: str) -> list[TrackMetadata]:
        """
        Fallback: Apple publishes an RSS feed for curated playlists, e.g. Top Charts.
        Returns up to ~25 tracks. Only works for Apple-owned playlists.
        """
        # Try fetching the iTunes URL for Apple-curated playlists (not user playlists)
        try:
            resp = self._session.get(
                _ITUNES_LOOKUP,
                params={"id": playlist_id.replace("pl.", ""), "entity": "song", "limit": 100},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.ok:
                results = resp.json().get("results", [])
                songs = [r for r in results if r.get("wrapperType") == "track" and r.get("kind") == "song"]
                if songs:
                    logger.info(f"[Apple] RSS fallback: found {len(songs)} tracks")
                    return [m for m in (self._item_to_metadata(s) for s in songs) if m]
        except Exception as e:
            logger.debug(f"[Apple] RSS fallback failed: {e}")

        return []

    # ── Developer token ───────────────────────────────────────────────────────

    def _get_developer_token(self) -> Optional[str]:
        """
        Get a developer token for the Apple Music Catalog API.

        Priority:
        1. Explicitly provided token (from config)
        2. Auto-extracted from the Apple Music web player JS bundle
           (Apple embeds a short-lived anonymous token there)
        3. None (caller falls back gracefully)
        """
        if self._dev_token:
            return self._dev_token

        return self._fetch_token_from_web_player()

    def _fetch_token_from_web_player(self) -> Optional[str]:
        """
        Extract the anonymous Apple Music API token from the web player bundle.
        Apple embeds a JWT in the main JS for anonymous catalog access.
        This is the same technique used by apple-music-metadata and similar libs.
        """
        try:
            # Step 1: Fetch the web player HTML to find the JS bundle URL
            resp = self._session.get(
                "https://music.apple.com/us/browse",
                timeout=(5, 10),   # (connect, read) — fail fast on blocked networks
            )
            if not resp.ok:
                return None

            # Step 2: Find the main JS bundle URL
            js_url_match = re.search(
                r'src="(/assets/index~[^"]+\.js)"',
                resp.text,
            )
            if not js_url_match:
                # Try alternate pattern (legacy)
                js_url_match = re.search(
                    r'src="(https://js-cdn\.music\.apple\.com/[^"]+index[^"]+\.js)"',
                    resp.text,
                )
            if not js_url_match:
                logger.debug("[Apple] Could not find JS bundle URL in web player HTML")
                return None

            js_path = js_url_match.group(1)
            js_url = f"https://music.apple.com{js_path}" if js_path.startswith("/") else js_path
            logger.debug(f"[Apple] Fetching JS bundle from: {js_url}")

            # Step 3: Extract the JWT from the JS
            js_resp = self._session.get(js_url, timeout=20)
            if not js_resp.ok:
                return None

            # Match JWT pattern (3 dot-separated base64url segments)
            token_match = re.search(
                r'"(eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)"',
                js_resp.text,
            )
            if not token_match:
                logger.debug("[Apple] No JWT token found in JS bundle")
                return None

            token = token_match.group(1)
            logger.debug("[Apple] Successfully extracted anonymous developer token from web player")
            return token

        except Exception as e:
            logger.debug(f"[Apple] Failed to auto-fetch developer token: {e}")
            return None

    # ── Data conversion ───────────────────────────────────────────────────────

    def _item_to_metadata(self, item: dict) -> Optional[TrackMetadata]:
        """Convert an iTunes API result item to TrackMetadata."""
        try:
            track_id = str(item.get("trackId", ""))
            title    = item.get("trackName", "")
            artist   = item.get("artistName", "")
            album    = item.get("collectionName", "")

            if not title or not artist:
                return None

            duration_ms  = item.get("trackTimeMillis")
            track_number = item.get("trackNumber")
            disc_number  = item.get("discNumber")
            total_tracks = item.get("trackCount")
            release_raw  = (item.get("releaseDate") or "")[:10]  # "YYYY-MM-DD"
            release_year = int(release_raw[:4]) if len(release_raw) >= 4 else None
            artwork_url  = self._upgrade_artwork_url(item.get("artworkUrl100", ""))

            # iTunes provides the ISRC via a different field in some responses
            # The lookup API doesn't reliably return ISRCs — the waterfall resolver
            # will use title+artist+duration matching instead.
            isrc = item.get("isrc") or None

            return TrackMetadata(
                title=title,
                artists=self._split_artists(artist),
                album=album,
                duration_ms=int(duration_ms) if duration_ms else None,
                isrc=isrc,
                track_number=track_number,
                disc_number=disc_number,
                total_tracks=total_tracks,
                release_date=release_raw or None,
                release_year=release_year,
                artwork_url=artwork_url,
            )
        except Exception as e:
            logger.debug(f"[Apple] Failed to parse iTunes item: {e}")
            return None

    def _catalog_item_to_metadata(self, attrs: dict) -> Optional[TrackMetadata]:
        """Convert an Apple Music Catalog API track attributes dict to TrackMetadata."""
        try:
            title  = attrs.get("name", "")
            artist = attrs.get("artistName", "")
            album  = attrs.get("albumName", "")

            if not title or not artist:
                return None

            duration_ms  = attrs.get("durationInMillis")
            track_number = attrs.get("trackNumber")
            disc_number  = attrs.get("discNumber")
            release_raw  = (attrs.get("releaseDate") or "")[:10]
            release_year = int(release_raw[:4]) if len(release_raw) >= 4 else None
            isrc         = attrs.get("isrc") or None

            # Artwork: raw 3000x3000cc lossless quality
            art_raw = attrs.get("artwork", {})
            artwork_url = None
            if art_raw:
                artwork_url = (
                    art_raw.get("url", "")
                    .replace("{w}", "3000")
                    .replace("{h}", "3000")
                )
                
            genres = attrs.get("genreNames", [])
            audio_traits = attrs.get("audioTraits", [])
            content_rating = attrs.get("contentRating")  # "explicit", "clean", or absent
            is_explicit = (
                True if content_rating == "explicit"
                else False if content_rating == "clean"
                else None
            )

            # Note: upc is usually not at the track level in catalog API (it's at album level)
            # but we pass it down by injecting it externally.

            return TrackMetadata(
                title=title,
                artists=self._split_artists(artist),
                album=album,
                duration_ms=int(duration_ms) if duration_ms else None,
                isrc=isrc,
                track_number=track_number,
                disc_number=disc_number,
                release_date=release_raw or None,
                release_year=release_year,
                artwork_url=artwork_url,
                genres=genres,
                audio_traits=audio_traits,
                is_explicit=is_explicit,
            )
        except Exception as e:
            logger.debug(f"[Apple] Failed to parse catalog item: {e}")
            return None

    # ── Artist discography ────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """
        Return artist metadata + full album list for the discography picker UI.
        Uses the iTunes Lookup API (no auth required).
        """
        # Extract artist ID and storefront from URL
        # Handles /artist/name/271256 (with slug) and /artist/271256 (bare ID)
        id_match = re.search(r"/artist/(?:[^/]+/)?(\d+)", url_or_id)
        if id_match:
            artist_id = id_match.group(1)
        elif url_or_id.strip().isdigit():
            artist_id = url_or_id.strip()
        else:
            raise ValueError(f"[Apple] Cannot parse artist ID from: {url_or_id}")

        sf_match = re.search(r"music\.apple\.com/([a-z]{2})/", url_or_id)
        storefront = sf_match.group(1) if sf_match else "us"

        # Fetch artist + albums via iTunes Lookup
        try:
            resp = self._session.get(
                _ITUNES_LOOKUP,
                params={"id": artist_id, "entity": "album", "limit": 200, "country": storefront},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"[Apple] iTunes artist lookup failed: {e}") from e

        results = resp.json().get("results", [])

        artist_item = next((r for r in results if r.get("wrapperType") == "artist"), None)
        artist_name = artist_item.get("artistName", "Unknown Artist") if artist_item else "Unknown Artist"

        # Try to get artist photo from Catalog API
        artwork_url = self._fetch_artist_artwork(artist_id, storefront)

        albums = []
        for item in results:
            if item.get("wrapperType") != "collection":
                continue
            release_raw = (item.get("releaseDate") or "")[:10]
            year = int(release_raw[:4]) if len(release_raw) >= 4 and release_raw[:4].isdigit() else None
            collection_id = str(item.get("collectionId", ""))
            name = item.get("collectionName", "")

            # iTunes marks singles as "Name - Single" and EPs as "Name - EP"
            if name.endswith(" - Single"):
                album_type = "single"
                name = name[: -len(" - Single")]
            elif name.endswith(" - EP"):
                album_type = "compilation"
                name = name[: -len(" - EP")]
            else:
                album_type = "album"

            art = self._upgrade_artwork_url(item.get("artworkUrl100", ""))
            album_url = f"https://music.apple.com/{storefront}/album/{collection_id}"
            albums.append({
                "id": collection_id,
                "url": album_url,
                "name": name,
                "type": album_type,
                "year": year,
                "track_count": item.get("trackCount", 0),
                "artwork_url": art,
            })

        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artwork_url": artwork_url,
            "albums": albums,
        }

    def search_artists(self, query: str, limit: int = 8) -> list[dict]:
        """Search Apple Music for artists by name via the iTunes Search API.

        Returns a list of artist dicts sorted by match_score descending:
        {artist_id, name, artwork_url, genres, followers, match_score, profile_url, source}
        """
        from antra.utils.matching import normalize, string_similarity
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            resp = self._session.get(
                _ITUNES_SEARCH,
                params={"term": query, "entity": "musicArtist", "limit": limit, "media": "music"},
                timeout=REQUEST_TIMEOUT,
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
            # Fetch artist photos in parallel via the Catalog API (requires developer token;
            # silently skipped per-artist if token is unavailable or request fails).
            artwork_by_id: dict[str, Optional[str]] = {}
            with ThreadPoolExecutor(max_workers=min(len(results), 4)) as pool:
                futures = {
                    pool.submit(self._fetch_artist_artwork, r["artist_id"], "us"): r["artist_id"]
                    for r in results
                }
                for fut in as_completed(futures):
                    aid = futures[fut]
                    try:
                        artwork_by_id[aid] = fut.result()
                    except Exception:
                        artwork_by_id[aid] = None
            for r in results:
                r["artwork_url"] = artwork_by_id.get(r["artist_id"])
            return results
        except Exception as e:
            logger.debug(f"[Apple] Artist search failed: {e}")
            return []

    def _fetch_artist_artwork(self, artist_id: str, storefront: str) -> Optional[str]:
        """Try to get artist photo from the Apple Music Catalog API."""
        token = self._get_developer_token()
        if not token:
            return None
        try:
            resp = self._session.get(
                f"{_AM_CATALOG.format(storefront=storefront)}/artists/{artist_id}",
                headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
                timeout=REQUEST_TIMEOUT,
            )
            if not resp.ok:
                return None
            data = resp.json().get("data", [])
            if not data:
                return None
            art = data[0].get("attributes", {}).get("artwork", {})
            url_template = art.get("url", "")
            if url_template:
                return url_template.replace("{w}", "400").replace("{h}", "400")
        except Exception as e:
            logger.debug(f"[Apple] Could not fetch artist artwork: {e}")
        return None

    @staticmethod
    def _split_artists(artist_name: str) -> list[str]:
        """Split a combined artist string into individual names.

        Apple Music / iTunes returns a single ``artistName`` string like
        ``"Future, Metro Boomin & The Weeknd"`` instead of a proper list.
        Splitting it lets downstream adapters search for just the primary
        artist name, which dramatically improves search accuracy.
        """
        if not artist_name:
            return ["Unknown Artist"]
        # Split on ' & ', ', ', ' and ', ' feat. ', ' feat ', ' ft. ', ' ft '
        parts = re.split(r'\s*[,&]\s*|\s+(?:and|feat\.?|ft\.?)\s+', artist_name, flags=re.IGNORECASE)
        artists = [p.strip() for p in parts if p.strip()]
        return artists if artists else [artist_name]

    @staticmethod
    def _upgrade_artwork_url(url: str) -> Optional[str]:
        """Upgrade iTunes thumbnail URL from 100x100 to 1200x1200."""
        if not url:
            return None
        return re.sub(r"\d+x\d+bb", "1200x1200bb", url)
