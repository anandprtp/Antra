"""
Apple Music user-library client for the desktop My Library screen.

Uses the same Apple Music web session that Antra already captures:
  - Authorization: Bearer ...
  - Music-User-Token: ...

The public Catalog API is enough for shared URLs, but the user's private
library requires the authenticated MusicKit "me/library" endpoints.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

APPLE_LIBRARY_SONGS_URL = "apple-music://library/songs"
APPLE_LIBRARY_PLAYLIST_URL_PREFIX = "apple-music://library/playlist/"
_LIBRARY_API_BASE = "https://api.music.apple.com/v1/me/library"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

_ALGO_NAMES = re.compile(
    r"favorites mix|new music mix|chill mix|friends mix|replay|personal station"
    r"|made for you|mix$|mix \d|station",
    re.IGNORECASE,
)


def is_apple_library_url(url: str) -> bool:
    return url.startswith(APPLE_LIBRARY_SONGS_URL) or url.startswith(APPLE_LIBRARY_PLAYLIST_URL_PREFIX)


def extract_apple_library_playlist_id(url: str) -> Optional[str]:
    if not url.startswith(APPLE_LIBRARY_PLAYLIST_URL_PREFIX):
        return None
    playlist_id = url[len(APPLE_LIBRARY_PLAYLIST_URL_PREFIX):].strip()
    return playlist_id or None


class AppleLibraryClient:
    """Fetch a user's Apple Music library (saved songs + library playlists)."""

    def __init__(self, authorization_token: str, music_user_token: str, storefront: str = "us"):
        auth = (authorization_token or "").strip()
        mut = (music_user_token or "").strip()
        if not auth:
            raise ValueError("Apple Music authorization token is required.")
        if not mut:
            raise ValueError("Apple Music user token is required.")

        self._auth = auth
        self._mut = mut
        self._storefront = (storefront or "us").strip().lower() or "us"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": self._auth,
            "Music-User-Token": self._mut,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Accept": "application/json",
            "User-Agent": _UA,
        })

    def get_library(self) -> dict:
        saved_songs_count = self._get_saved_songs_count()
        playlists = self._get_all_playlists()
        return {
            "saved_songs_count": saved_songs_count,
            "playlists": playlists,
        }

    def get_saved_songs_tracks(self, page_callback=None) -> list[TrackMetadata]:
        tracks: list[TrackMetadata] = []
        for item in self._iter_collection("/songs", params={"limit": 100}):
            meta = self._library_song_to_metadata(item)
            if not meta:
                continue
            meta.playlist_name = "Saved Songs"
            meta.playlist_position = len(tracks) + 1
            meta.request_kind = "playlist"
            tracks.append(meta)
            if page_callback and len(tracks) % 100 == 0:
                try:
                    page_callback(list(tracks))
                except Exception:
                    pass
        if page_callback and tracks:
            try:
                page_callback(list(tracks))
            except Exception:
                pass
        return tracks

    def get_library_playlist_tracks(self, playlist_id: str, page_callback=None) -> list[TrackMetadata]:
        playlist_name, playlist_artwork = self._get_library_playlist_meta(playlist_id)
        tracks: list[TrackMetadata] = []
        path = f"/playlists/{playlist_id}/tracks"
        for item in self._iter_collection(path, params={"limit": 100}):
            meta = self._library_song_to_metadata(item)
            if not meta:
                continue
            meta.playlist_name = playlist_name
            meta.playlist_position = len(tracks) + 1
            meta.playlist_artwork_url = playlist_artwork
            meta.request_kind = "playlist"
            tracks.append(meta)
            if page_callback and len(tracks) % 100 == 0:
                try:
                    page_callback(list(tracks))
                except Exception:
                    pass
        if page_callback and tracks:
            try:
                page_callback(list(tracks))
            except Exception:
                pass
        return tracks

    def _get_saved_songs_count(self) -> int:
        try:
            payload = self._get_json("/songs", params={"limit": 1})
        except Exception as exc:
            logger.warning("[AppleLibrary] saved songs count failed: %s", exc)
            return 0

        meta = payload.get("meta") or {}
        total = meta.get("total")
        if isinstance(total, int):
            return total
        items = payload.get("data") or []
        return len(items)

    def _get_all_playlists(self) -> list[dict]:
        playlists: list[dict] = []
        for item in self._iter_collection("/playlists", params={"limit": 100}):
            summary = self._playlist_summary(item)
            if summary:
                playlists.append(summary)
        playlists.sort(key=lambda p: (0 if p["is_algorithmic"] else 1, p["name"].lower()))
        return playlists

    def _playlist_summary(self, item: dict) -> Optional[dict]:
        attrs = item.get("attributes") or {}
        name = (attrs.get("name") or "").strip()
        if not name:
            return None

        owner_name = (attrs.get("curatorName") or attrs.get("playlistType") or "Apple Music").strip()
        description = self._extract_description(attrs)
        image_url = self._artwork_url(attrs.get("artwork") or {})
        track_count = (
            attrs.get("trackCount")
            or ((item.get("relationships") or {}).get("tracks") or {}).get("meta", {}).get("total")
            or 0
        )

        is_algorithmic = bool(_ALGO_NAMES.search(name))
        owner_lower = owner_name.lower()
        if owner_lower == "apple music" and _ALGO_NAMES.search(description or name):
            is_algorithmic = True

        playlist_id = str(item.get("id") or "").strip()
        if not playlist_id:
            return None

        return {
            "id": playlist_id,
            "name": name,
            "url": f"{APPLE_LIBRARY_PLAYLIST_URL_PREFIX}{playlist_id}",
            "image_url": image_url,
            "track_count": int(track_count or 0),
            "owner_name": owner_name,
            "is_algorithmic": is_algorithmic,
            "description": description,
        }

    def _get_library_playlist_meta(self, playlist_id: str) -> tuple[str, Optional[str]]:
        try:
            payload = self._get_json(f"/playlists/{playlist_id}")
            data = payload.get("data") or []
            if data:
                attrs = data[0].get("attributes") or {}
                name = (attrs.get("name") or "Apple Music Playlist").strip() or "Apple Music Playlist"
                artwork = self._artwork_url(attrs.get("artwork") or {})
                return name, artwork
        except Exception as exc:
            logger.debug("[AppleLibrary] playlist meta lookup failed for %s: %s", playlist_id, exc)
        return "Apple Music Playlist", None

    def _iter_collection(self, path: str, params: Optional[dict] = None):
        next_ref: Optional[str] = path
        next_params = dict(params or {})
        pages = 0

        while next_ref and pages < 200:
            payload = self._get_json(next_ref, params=next_params)
            next_params = None
            for item in payload.get("data") or []:
                yield item

            next_ref = payload.get("next")
            pages += 1

    def _get_json(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{_LIBRARY_API_BASE}{path_or_url}"
        resp = self._session.get(url, params=params, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Apple Music session expired. Reconnect your account in Settings.")
        if resp.status_code == 403:
            raise RuntimeError("Apple Music library access was denied for this account.")
        resp.raise_for_status()
        return resp.json()

    def _library_song_to_metadata(self, item: dict) -> Optional[TrackMetadata]:
        attrs = item.get("attributes") or {}
        title = (attrs.get("name") or "").strip()
        artist = (attrs.get("artistName") or "").strip()
        if not title or not artist:
            return None

        release_date = (attrs.get("releaseDate") or "")[:10] or None
        release_year = None
        if release_date:
            try:
                release_year = int(release_date[:4])
            except ValueError:
                release_year = None

        play_params = attrs.get("playParams") or {}
        relationships = item.get("relationships") or {}
        catalog_data = ((relationships.get("catalog") or {}).get("data") or [])
        catalog_id = (
            play_params.get("catalogId")
            or play_params.get("id")
            or (catalog_data[0].get("id") if catalog_data else None)
            or item.get("id")
        )

        content_rating = attrs.get("contentRating")
        is_explicit = (
            True if content_rating == "explicit"
            else False if content_rating == "clean"
            else None
        )

        return TrackMetadata(
            title=title,
            artists=self._split_artists(artist),
            album=(attrs.get("albumName") or "").strip() or "Saved Songs",
            duration_ms=attrs.get("durationInMillis"),
            isrc=attrs.get("isrc") or None,
            track_number=attrs.get("trackNumber"),
            disc_number=attrs.get("discNumber"),
            release_date=release_date,
            release_year=release_year,
            artwork_url=self._artwork_url(attrs.get("artwork") or {}),
            genres=attrs.get("genreNames") or [],
            audio_traits=attrs.get("audioTraits") or [],
            is_explicit=is_explicit,
            apple_music_id=str(catalog_id) if catalog_id else None,
        )

    @staticmethod
    def _extract_description(attrs: dict) -> str:
        description = attrs.get("description")
        if isinstance(description, dict):
            return (
                description.get("standard")
                or description.get("short")
                or description.get("editorialNotes")
                or ""
            ).strip()
        return str(description or "").strip()

    @staticmethod
    def _artwork_url(artwork: dict) -> Optional[str]:
        if not isinstance(artwork, dict):
            return None
        template = artwork.get("url") or ""
        if not template:
            return None
        width = str(artwork.get("width") or 1200)
        height = str(artwork.get("height") or 1200)
        return template.replace("{w}", width).replace("{h}", height)

    @staticmethod
    def _split_artists(artist_name: str) -> list[str]:
        if not artist_name:
            return ["Unknown Artist"]
        parts = re.split(r"\s*[,&]\s*|\s+(?:and|feat\.?|ft\.?)\s+", artist_name, flags=re.IGNORECASE)
        artists = [part.strip() for part in parts if part.strip()]
        return artists or [artist_name]
