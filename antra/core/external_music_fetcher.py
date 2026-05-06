"""
Metadata fetchers for non-Spotify streaming URLs.

These fetchers normalize TIDAL, Qobuz, and Deezer album/playlist/track links into
TrackMetadata so the existing Antra resolver/download engine can process them.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from antra.core.config import Config
from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

_DEEZER_API = "https://api.deezer.com"
_ODESLI_API = "https://api.song.link/v1-alpha.1/links"


def is_tidal_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("tidal.com") or host.endswith("listen.tidal.com")


def is_qobuz_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("qobuz.com") or host == "open.qobuz.com"


def is_deezer_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("deezer.com") or host in {"deezer.page.link", "link.deezer.com"}


class ExternalMusicFetcher:
    """Resolve TIDAL, Qobuz, and Deezer URLs to normalized track metadata."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/html, */*",
            }
        )

    def fetch(self, url: str) -> list[TrackMetadata]:
        if is_deezer_url(url):
            return self._fetch_deezer(url)
        if is_qobuz_url(url):
            return self._fetch_qobuz(url)
        if is_tidal_url(url):
            return self._fetch_tidal(url)
        raise ValueError(f"Unsupported external music URL: {url}")

    @staticmethod
    def _stamp_request_kind(tracks: list[TrackMetadata], kind: str) -> None:
        for track in tracks:
            track.request_kind = kind

    # -- Deezer -------------------------------------------------------------

    def _fetch_deezer(self, url: str) -> list[TrackMetadata]:
        kind, item_id = self._extract_deezer_kind_id(url)
        if kind == "track":
            data = self._deezer_get(f"track/{item_id}")
            tracks = [self._deezer_track(data)]
            self._stamp_request_kind(tracks, "track")
            return tracks
        if kind == "album":
            data = self._deezer_get(f"album/{item_id}")
            album_title = data.get("title") or ""
            album_artist = (data.get("artist") or {}).get("name") or ""
            artwork = self._best_deezer_cover(data)
            tracks = ((data.get("tracks") or {}).get("data") or [])
            total_tracks = int(data.get("nb_tracks") or len(tracks) or 0) or None
            tracks = [
                self._deezer_track(
                    item,
                    album_title=album_title,
                    album_artist=album_artist,
                    artwork_url=artwork,
                    release_date=data.get("release_date") or None,
                    total_tracks=total_tracks,
                    position=index,
                )
                for index, item in enumerate(tracks, 1)
            ]
            self._stamp_request_kind(tracks, "album")
            return tracks
        if kind == "playlist":
            data = self._deezer_get(f"playlist/{item_id}")
            playlist_name = data.get("title") or ""
            playlist_owner = (data.get("creator") or {}).get("name") or ""
            playlist_description = data.get("description") or ""
            playlist_artwork = self._best_deezer_cover(data)
            tracks = ((data.get("tracks") or {}).get("data") or [])
            tracks = [
                self._deezer_track(
                    item,
                    playlist_name=playlist_name,
                    playlist_owner=playlist_owner,
                    playlist_description=playlist_description,
                    playlist_artwork_url=playlist_artwork,
                    position=index,
                )
                for index, item in enumerate(tracks, 1)
            ]
            self._stamp_request_kind(tracks, "playlist")
            return tracks
        raise ValueError(f"Unsupported Deezer URL type: {kind}")

    def _deezer_get(self, path: str) -> dict[str, Any]:
        resp = self._session.get(f"{_DEEZER_API}/{path}", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Deezer API error: {data['error']}")
        return data

    def _extract_deezer_kind_id(self, url: str) -> tuple[str, str]:
        resolved = self._resolve_redirect(url)
        patterns = (
            r"deezer\.com/(?:[a-z]{2}/)?(?P<kind>album|playlist|track)/(?P<id>\d+)",
            r"deezer\.page\.link/.*?(?P<kind>album|playlist|track)/(?P<id>\d+)",
        )
        for pattern in patterns:
            match = re.search(pattern, resolved, re.IGNORECASE)
            if match:
                return match.group("kind").lower(), match.group("id")
        if "deezer.com/soon" in resolved:
            fallback = self._resolve_deezer_shortlink_via_songlink(url)
            if fallback:
                return fallback
        raise ValueError(
            "Unsupported Deezer URL. Expected deezer.com/album/..., /playlist/..., or /track/..."
        )

    def _resolve_deezer_shortlink_via_songlink(self, url: str) -> Optional[tuple[str, str]]:
        params = {"url": url}
        api_key = getattr(self.cfg, "odesli_api_key", "") or ""
        if api_key:
            params["key"] = api_key
        try:
            resp = self._session.get(_ODESLI_API, params=params, timeout=15)
            if not resp.ok:
                logger.debug(
                    "[ExternalMusic] song.link Deezer short-link lookup failed: HTTP %s",
                    resp.status_code,
                )
                return None
            data = resp.json()
        except Exception as exc:
            logger.debug(f"[ExternalMusic] song.link Deezer short-link lookup failed: {exc}")
            return None

        entity_uid = str(data.get("entityUniqueId") or "")
        if not entity_uid.startswith("DEEZER_"):
            return None

        entity = (data.get("entitiesByUniqueId") or {}).get(entity_uid) or {}
        raw_id = str(entity.get("id") or "")
        if not raw_id:
            return None

        if entity_uid.startswith("DEEZER_SONG::"):
            return ("track", raw_id)
        if entity_uid.startswith("DEEZER_ALBUM::"):
            return ("album", raw_id)
        if entity_uid.startswith("DEEZER_PLAYLIST::"):
            return ("playlist", raw_id)
        return None

    def _deezer_track(
        self,
        item: dict[str, Any],
        *,
        album_title: str = "",
        album_artist: str = "",
        artwork_url: Optional[str] = None,
        release_date: Optional[str] = None,
        total_tracks: Optional[int] = None,
        playlist_name: Optional[str] = None,
        playlist_owner: Optional[str] = None,
        playlist_description: Optional[str] = None,
        playlist_artwork_url: Optional[str] = None,
        position: Optional[int] = None,
    ) -> TrackMetadata:
        artist = (item.get("artist") or {}).get("name") or ""
        album = item.get("album") or {}
        return TrackMetadata(
            title=item.get("title") or item.get("title_short") or "",
            artists=[artist] if artist else [],
            album=album_title or album.get("title") or "",
            album_artists=[album_artist] if album_artist else [],
            artwork_url=artwork_url or self._best_deezer_cover(album),
            playlist_name=playlist_name,
            playlist_owner=playlist_owner,
            playlist_description=playlist_description,
            playlist_position=position if playlist_name else None,
            playlist_artwork_url=playlist_artwork_url,
            release_date=release_date,
            release_year=int(release_date[:4]) if release_date and release_date[:4].isdigit() else None,
            track_number=item.get("track_position") or (position if album_title else None),
            disc_number=item.get("disk_number") or None,
            total_tracks=total_tracks,
            duration_ms=(int(item["duration"]) * 1000) if item.get("duration") else None,
            isrc=item.get("isrc") or None,
            deezer_track_id=str(item.get("id")) if item.get("id") else None,
            album_id=str(album.get("id")) if album.get("id") else None,
            spotify_url=None,
            is_explicit=bool(item.get("explicit_lyrics")) if "explicit_lyrics" in item else None,
        )

    @staticmethod
    def _best_deezer_cover(data: dict[str, Any]) -> Optional[str]:
        return (
            data.get("cover_xl")
            or data.get("picture_xl")
            or data.get("cover_big")
            or data.get("picture_big")
            or data.get("cover_medium")
            or data.get("picture_medium")
        )

    # -- Qobuz --------------------------------------------------------------

    def _fetch_qobuz(self, url: str) -> list[TrackMetadata]:
        kind, item_id = self._extract_qobuz_kind_id(url)

        # Try local Qobuz credentials first
        try:
            adapter = self._qobuz_adapter()
            if kind == "track":
                data = adapter._api_get("track/get", params={"track_id": item_id}).json()
                tracks = [self._qobuz_track(data)]
                self._stamp_request_kind(tracks, "track")
                return tracks
            if kind == "album":
                data = adapter._api_get("album/get", params={"album_id": item_id}).json()
                album_title = data.get("title") or ""
                album_artist = ((data.get("artist") or {}).get("name") or "")
                artwork = self._qobuz_image(data.get("image") or {})
                tracks = ((data.get("tracks") or {}).get("items") or [])
                tracks = [
                    self._qobuz_track(
                        item,
                        album_title=album_title,
                        album_artist=album_artist,
                        artwork_url=artwork,
                        release_date=data.get("release_date_original") or data.get("release_date_download"),
                        total_tracks=data.get("tracks_count") or len(tracks),
                    )
                    for item in tracks
                ]
                self._stamp_request_kind(tracks, "album")
                return tracks
            if kind == "playlist":
                data = adapter._api_get("playlist/get", params={"playlist_id": item_id}).json()
                playlist_name = data.get("name") or data.get("title") or ""
                owner = ((data.get("owner") or {}).get("name") or "")
                artwork = self._qobuz_image(data.get("image_rectangle") or data.get("image") or {})
                tracks = ((data.get("tracks") or {}).get("items") or [])
                tracks = [
                    self._qobuz_track(
                        item,
                        playlist_name=playlist_name,
                        playlist_owner=owner,
                        playlist_description=data.get("description") or "",
                        playlist_artwork_url=artwork,
                        playlist_position=index,
                    )
                    for index, item in enumerate(tracks, 1)
                ]
                self._stamp_request_kind(tracks, "playlist")
                return tracks
        except Exception as e:
            err_str = str(e).lower()
            if not any(kw in err_str for kw in (
                "requires qobuz credentials", "not configured", "is_available",
                "empty", "auth", "token", "401", "403", "login",
            )):
                raise
            logger.debug("[ExternalMusic] Qobuz local credentials unavailable (%s) — trying VPS mirror", e)

        return self._fetch_qobuz_via_mirror(kind, item_id)

    def _fetch_qobuz_via_mirror(self, kind: str, item_id: str) -> list[TrackMetadata]:
        """Fetch Qobuz metadata via the VPS mirror — no local credentials required."""
        from antra.core.endpoint_manifest import load_endpoint_manifest

        manifest = load_endpoint_manifest()
        mirror_url = (getattr(manifest, "mirror_qobuz", "") or "").rstrip("/")
        api_key = (getattr(manifest, "api_key", "") or "").strip()
        if not api_key:
            api_key = (getattr(self.cfg, "antra_api_key", "") or "").strip()

        if not mirror_url:
            raise RuntimeError(
                "No Qobuz mirror URL available. "
                "Add Qobuz credentials in Settings, or ensure the endpoint manifest is reachable."
            )

        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        try:
            if kind == "album":
                r = self._session.get(
                    f"{mirror_url}/api/album/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Album {item_id} not found on Qobuz")
                r.raise_for_status()
                data = r.json()
            elif kind == "track":
                r = self._session.get(
                    f"{mirror_url}/api/meta/track/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Track {item_id} not found on Qobuz")
                r.raise_for_status()
                tracks = [self._qobuz_track(r.json())]
                self._stamp_request_kind(tracks, "track")
                return tracks
            elif kind == "playlist":
                r = self._session.get(
                    f"{mirror_url}/api/playlist/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Playlist {item_id} not found on Qobuz")
                r.raise_for_status()
                data = r.json()
                playlist_name = data.get("title", "")
                playlist_owner = data.get("owner") or ""
                playlist_description = data.get("description", "") or ""
                playlist_artwork_url = data.get("artwork_url")
                tracks = data.get("tracks") or []
                tracks = [
                    self._qobuz_track(
                        item,
                        playlist_name=playlist_name,
                        playlist_owner=playlist_owner,
                        playlist_description=playlist_description,
                        playlist_artwork_url=playlist_artwork_url,
                        playlist_position=index,
                    )
                    for index, item in enumerate(tracks, 1)
                ]
                self._stamp_request_kind(tracks, "playlist")
                return tracks
            else:
                raise ValueError(f"Unsupported Qobuz URL type: {kind}")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Could not fetch Qobuz metadata via mirror for {kind}/{item_id}: {e}. "
                "Add Qobuz credentials in Settings for reliable access."
            ) from e

        album_title = data.get("title", "")
        album_artists = data.get("album_artists") or []
        album_artist = album_artists[0] if album_artists else ""
        artwork_url = data.get("artwork_url")
        release_date = data.get("release_date") or ""
        release_year = data.get("release_year") or (
            int(release_date[:4]) if release_date and release_date[:4].isdigit() else None
        )
        total_tracks = data.get("total_tracks") or len(data.get("tracks", []))

        result = []
        for t in data.get("tracks") or []:
            rd = release_date
            ry = release_year
            result.append(TrackMetadata(
                title=t.get("title") or "",
                artists=t.get("artists") or [],
                album=album_title,
                album_artists=album_artists,
                artwork_url=artwork_url,
                release_date=rd or None,
                release_year=ry,
                track_number=t.get("track_number"),
                disc_number=t.get("disc_number"),
                total_tracks=total_tracks,
                duration_ms=t.get("duration_ms") or None,
                isrc=t.get("isrc") or None,
                album_id=t.get("album_id") or item_id,
                is_explicit=t.get("explicit"),
            ))
        self._stamp_request_kind(result, "album")
        return result

    def _qobuz_adapter(self):
        from antra.sources.qobuz import QobuzAdapter

        adapter = QobuzAdapter(
            email=getattr(self.cfg, "qobuz_email", ""),
            password=getattr(self.cfg, "qobuz_password", ""),
            app_id=getattr(self.cfg, "qobuz_app_id", ""),
            app_secret=getattr(self.cfg, "qobuz_app_secret", ""),
            user_auth_token=getattr(self.cfg, "qobuz_user_auth_token", ""),
        )
        if not adapter.is_available():
            raise RuntimeError(
                "Qobuz link fetching requires Qobuz credentials or a user auth token in Settings."
            )
        return adapter

    @staticmethod
    def _extract_qobuz_kind_id(url: str) -> tuple[str, str]:
        match = re.search(r"(?:open|play)\.qobuz\.com/(?P<kind>album|playlist|track)/(?P<id>[\w-]+)", url, re.IGNORECASE)
        if match:
            return match.group("kind").lower(), match.group("id")
        match = re.search(r"qobuz\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?P<kind>album|playlist|track)/.+/(?P<id>[\w-]+)(?:[/?#]|$)", url, re.IGNORECASE)
        if match:
            return match.group("kind").lower(), match.group("id")
        match = re.search(r"qobuz\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?P<kind>album|playlist|track)/(?P<id>[\w-]+)(?:[/?#]|$)", url, re.IGNORECASE)
        if match:
            return match.group("kind").lower(), match.group("id")
        raise ValueError(
            "Unsupported Qobuz URL. Expected play.qobuz.com/album/..., open.qobuz.com/..., or qobuz.com/.../album|playlist|track/..."
        )

    def _qobuz_track(
        self,
        item: dict[str, Any],
        *,
        album_title: str = "",
        album_artist: str = "",
        artwork_url: Optional[str] = None,
        release_date: Optional[str] = None,
        total_tracks: Optional[int] = None,
        playlist_name: Optional[str] = None,
        playlist_owner: Optional[str] = None,
        playlist_description: Optional[str] = None,
        playlist_artwork_url: Optional[str] = None,
        playlist_position: Optional[int] = None,
    ) -> TrackMetadata:
        performer = item.get("performer") or {}
        raw_album = item.get("album")
        album = raw_album if isinstance(raw_album, dict) else {}
        artist_list = item.get("artists") or []
        artist_name = (
            performer.get("name")
            or ((item.get("artist") or {}).get("name") or "")
            or (artist_list[0] if artist_list else "")
        )
        album_title_value = album_title or (raw_album if isinstance(raw_album, str) else album.get("title") or "")
        album_artists_value = (
            [album_artist] if album_artist else (item.get("album_artists") or [])
        )
        return TrackMetadata(
            title=item.get("title") or "",
            artists=[artist_name] if artist_name else [],
            album=album_title_value,
            album_artists=album_artists_value,
            artwork_url=artwork_url or self._qobuz_image(album.get("image") or item.get("image") or {}),
            playlist_name=playlist_name,
            playlist_owner=playlist_owner,
            playlist_description=playlist_description,
            playlist_position=playlist_position,
            playlist_artwork_url=playlist_artwork_url,
            release_date=release_date or item.get("release_date_original") or item.get("release_date_download"),
            release_year=int(_rd[:4]) if (_rd := (release_date or item.get("release_date_original") or item.get("release_date_download") or "")) and _rd[:4].isdigit() else None,
            track_number=item.get("track_number") or item.get("media_number"),
            disc_number=item.get("media_number") or None,
            total_tracks=total_tracks,
            duration_ms=(int(item["duration"]) * 1000) if item.get("duration") else None,
            isrc=item.get("isrc") or None,
            album_id=str(album.get("id")) if album.get("id") else None,
            upc=album.get("upc") or None,
            is_explicit=item.get("parental_warning") if isinstance(item.get("parental_warning"), bool) else None,
        )

    @staticmethod
    def _qobuz_image(image: dict[str, Any]) -> Optional[str]:
        return image.get("large") or image.get("extralarge") or image.get("small") or image.get("thumbnail")

    # -- TIDAL --------------------------------------------------------------

    def _fetch_tidal(self, url: str) -> list[TrackMetadata]:
        kind, item_id = self._extract_tidal_kind_id(url)

        # Try local Tidal session first (if configured in Settings)
        try:
            session = self._tidal_session()
            if kind == "track":
                tracks = [self._tidal_track(session.track(int(item_id)))]
                self._stamp_request_kind(tracks, "track")
                return tracks
            if kind == "album":
                album = session.album(int(item_id))
                tracks = album.tracks()
                result = [
                    self._tidal_track(
                        track,
                        album_title=getattr(album, "name", "") or "",
                        album_artist=self._tidal_artist_name(album),
                        artwork_url=self._tidal_image(album),
                        release_date=str(getattr(album, "release_date", "") or "") or None,
                        total_tracks=len(tracks),
                    )
                    for track in tracks
                ]
                self._stamp_request_kind(result, "album")
                return result
            if kind == "playlist":
                playlist = session.playlist(item_id)
                tracks = playlist.tracks()
                result = [
                    self._tidal_track(
                        track,
                        playlist_name=getattr(playlist, "name", "") or "",
                        playlist_description=getattr(playlist, "description", "") or "",
                        playlist_artwork_url=self._tidal_image(playlist),
                        playlist_position=index,
                    )
                    for index, track in enumerate(tracks, 1)
                ]
                self._stamp_request_kind(result, "playlist")
                return result
        except Exception as e:
            err_str = str(e).lower()
            # Only fall through to public API on auth/config errors — not on network errors
            if not any(kw in err_str for kw in (
                "empty", "not configured", "session", "login", "auth",
                "token", "credential", "401", "403",
            )):
                raise
            logger.debug("[ExternalMusic] Tidal local session unavailable (%s) — trying public API", e)

        # Fallback: public Tidal API (no auth required for metadata)
        return self._fetch_tidal_public(kind, item_id)

    def _fetch_tidal_public(self, kind: str, item_id: str) -> list[TrackMetadata]:
        """Fetch Tidal metadata via the VPS mirror server — no local session required."""
        from antra.core.endpoint_manifest import load_endpoint_manifest

        manifest = load_endpoint_manifest()
        mirror_url = (getattr(manifest, "mirror_tidal", "") or "").rstrip("/")
        api_key = (getattr(manifest, "api_key", "") or "").strip()
        if not api_key:
            api_key = (getattr(self.cfg, "antra_api_key", "") or "").strip()

        if not mirror_url:
            raise RuntimeError(
                "No Tidal mirror URL available. "
                "Connect a Tidal account in Settings, or ensure the endpoint manifest is reachable."
            )

        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        try:
            if kind == "album":
                r = self._session.get(
                    f"{mirror_url}/api/album/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Album {item_id} not found on Tidal")
                r.raise_for_status()
                data = r.json()

                album_title = data.get("title", "")
                album_artists = data.get("album_artists") or []
                album_artist = album_artists[0] if album_artists else ""
                artwork_url = data.get("artwork_url")
                release_date = data.get("release_date") or ""
                release_year = int(release_date[:4]) if release_date and release_date[:4].isdigit() else None
                total_tracks = data.get("total_tracks") or len(data.get("tracks", []))

                result = []
                for t in data.get("tracks") or []:
                    rd = release_date
                    ry = release_year
                    # artists may be a list of strings or list of dicts
                    raw_artists = t.get("artists") or []
                    track_artists = [
                        (a if isinstance(a, str) else a.get("name", ""))
                        for a in raw_artists if a
                    ]
                    result.append(TrackMetadata(
                        title=t.get("title") or "",
                        artists=track_artists,
                        album=album_title,
                        album_artists=album_artists,
                        artwork_url=artwork_url,
                        release_date=rd or None,
                        release_year=ry,
                        track_number=t.get("track_number"),
                        disc_number=t.get("disc_number"),
                        total_tracks=total_tracks,
                        duration_ms=t.get("duration_ms") or None,
                        isrc=t.get("isrc") or None,
                        album_id=t.get("album_id") or str(item_id),
                        is_explicit=t.get("explicit"),
                    ))
                self._stamp_request_kind(result, "album")
                return result

            if kind == "track":
                r = self._session.get(
                    f"{mirror_url}/api/meta/track/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Track {item_id} not found on Tidal")
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"Unexpected response from mirror for track {item_id}: {str(data)[:200]}"
                    )
                if data.get("error") or data.get("status") == "error":
                    raise RuntimeError(
                        f"Mirror error for track {item_id}: {data.get('error') or data.get('message', 'unknown')}"
                    )
                tracks = [self._tidal_track_from_public(data)]
                self._stamp_request_kind(tracks, "track")
                return tracks

            if kind == "playlist":
                r = self._session.get(
                    f"{mirror_url}/api/playlist/{item_id}",
                    headers=headers, timeout=20,
                )
                if r.status_code == 404:
                    raise RuntimeError(f"Playlist {item_id} not found on Tidal")
                r.raise_for_status()
                data = r.json()
                playlist_name = data.get("title", "")
                playlist_description = data.get("description", "") or ""
                playlist_artwork_url = data.get("artwork_url")
                tracks = data.get("tracks") or []
                tracks = [
                    self._tidal_track_from_public(
                        item,
                        playlist_name=playlist_name,
                        playlist_description=playlist_description,
                        playlist_artwork_url=playlist_artwork_url,
                        playlist_position=index,
                    )
                    for index, item in enumerate(tracks, 1)
                ]
                self._stamp_request_kind(tracks, "playlist")
                return tracks

        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Could not fetch Tidal metadata via mirror for {kind}/{item_id}. "
                f"Connect a Tidal account in Settings for reliable access. Error: {e}"
            ) from e

        raise ValueError(f"Unsupported TIDAL URL type: {kind}")

    def _tidal_track_from_public(
        self,
        data: dict,
        *,
        album_title: str = "",
        album_artist: str = "",
        artwork_url: Optional[str] = None,
        release_date: Optional[str] = None,
        total_tracks: Optional[int] = None,
        playlist_name: Optional[str] = None,
        playlist_description: Optional[str] = None,
        playlist_artwork_url: Optional[str] = None,
        playlist_position: Optional[int] = None,
    ) -> TrackMetadata:
        """Convert a public Tidal API track object to TrackMetadata."""
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict from Tidal API, got {type(data).__name__}: {str(data)[:200]}")
        album_raw = data.get("album") or {}
        album = album_raw if isinstance(album_raw, dict) else {}
        artists_raw = data.get("artists") or []
        artists = []
        for a in artists_raw:
            if isinstance(a, dict):
                name = a.get("name", "")
            elif isinstance(a, str):
                name = a
            else:
                name = ""
            if name:
                artists.append(name)
        if not artists:
            artist_raw = data.get("artist") or {}
            if isinstance(artist_raw, dict):
                name = artist_raw.get("name", "")
            elif isinstance(artist_raw, str):
                name = artist_raw
            else:
                name = ""
            if name:
                artists = [name]

        rd = release_date or album.get("releaseDate") or ""
        release_year = int(rd[:4]) if rd and rd[:4].isdigit() else None

        # Cover art from track's album if not provided
        if not artwork_url:
            cover = album.get("cover", "")
            if cover:
                artwork_url = f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg"

        return TrackMetadata(
            title=data.get("title") or "",
            artists=artists,
            album=album_title or album.get("title") or "",
            album_artists=[album_artist] if album_artist else [],
            artwork_url=artwork_url,
            playlist_name=playlist_name,
            playlist_description=playlist_description,
            playlist_artwork_url=playlist_artwork_url,
            playlist_position=playlist_position,
            release_date=rd or None,
            release_year=release_year,
            track_number=data.get("trackNumber"),
            disc_number=data.get("volumeNumber"),
            total_tracks=total_tracks,
            duration_ms=(data.get("duration") or 0) * 1000 or None,
            isrc=data.get("isrc") or None,
            album_id=str(album.get("id")) if album.get("id") else None,
            is_explicit=data.get("explicit"),
        )

    def _tidal_session(self):
        from antra.sources.tidal import TidalAdapter

        adapter = TidalAdapter(
            email=getattr(self.cfg, "tidal_email", ""),
            password=getattr(self.cfg, "tidal_password", ""),
            enabled=True,
            auth_mode=getattr(self.cfg, "tidal_auth_mode", "session_json"),
            session_json=getattr(self.cfg, "tidal_session_json", ""),
            access_token=getattr(self.cfg, "tidal_access_token", ""),
            refresh_token=getattr(self.cfg, "tidal_refresh_token", ""),
            session_id=getattr(self.cfg, "tidal_session_id", ""),
            token_type=getattr(self.cfg, "tidal_token_type", "Bearer"),
        )
        return adapter._get_session()

    @staticmethod
    def _extract_tidal_kind_id(url: str) -> tuple[str, str]:
        match = re.search(r"(?:listen\.)?tidal\.com/(?:browse/)?(?P<kind>album|playlist|track)/(?P<id>[\w-]+)", url, re.IGNORECASE)
        if match:
            return match.group("kind").lower(), match.group("id")
        raise ValueError(
            "Unsupported TIDAL URL. Expected tidal.com/browse/album/..., /playlist/..., or /track/..."
        )

    def _tidal_track(
        self,
        track: Any,
        *,
        album_title: str = "",
        album_artist: str = "",
        artwork_url: Optional[str] = None,
        release_date: Optional[str] = None,
        total_tracks: Optional[int] = None,
        playlist_name: Optional[str] = None,
        playlist_description: Optional[str] = None,
        playlist_artwork_url: Optional[str] = None,
        playlist_position: Optional[int] = None,
    ) -> TrackMetadata:
        album = getattr(track, "album", None)
        return TrackMetadata(
            title=getattr(track, "name", "") or "",
            artists=self._tidal_artist_names(track),
            album=album_title or getattr(album, "name", "") or "",
            album_artists=[album_artist] if album_artist else [],
            artwork_url=artwork_url or self._tidal_image(album),
            playlist_name=playlist_name,
            playlist_description=playlist_description,
            playlist_position=playlist_position,
            playlist_artwork_url=playlist_artwork_url,
            release_date=release_date or str(getattr(track, "release_date", "") or "") or None,
            release_year=int(_rd[:4]) if (_rd := (release_date or str(getattr(track, "release_date", "") or "") or "")) and _rd[:4].isdigit() else None,
            track_number=getattr(track, "track_num", None) or getattr(track, "track_number", None),
            disc_number=getattr(track, "volume_num", None) or getattr(track, "disc_number", None),
            total_tracks=total_tracks,
            duration_ms=(int(getattr(track, "duration", 0)) * 1000) if getattr(track, "duration", None) else None,
            isrc=getattr(track, "isrc", None) or None,
            album_id=str(getattr(album, "id", "")) if getattr(album, "id", None) else None,
            is_explicit=getattr(track, "explicit", None),
        )

    @staticmethod
    def _tidal_artist_names(item: Any) -> list[str]:
        artists = getattr(item, "artists", None) or []
        names = [getattr(artist, "name", "") for artist in artists]
        if names:
            return [name for name in names if name]
        artist = getattr(item, "artist", None)
        name = getattr(artist, "name", "") if artist else ""
        return [name] if name else []

    def _tidal_artist_name(self, item: Any) -> str:
        names = self._tidal_artist_names(item)
        return names[0] if names else ""

    @staticmethod
    def _tidal_image(item: Any) -> Optional[str]:
        if item is None:
            return None
        for size in (1280, 640, 320):
            try:
                url = item.image(size)
                if url:
                    return url
            except Exception:
                continue
        return None

    # -- Shared -------------------------------------------------------------

    def _resolve_redirect(self, url: str) -> str:
        if not any(host in url for host in ("deezer.page.link", "link.deezer.com")):
            return url
        try:
            resp = self._session.get(url, timeout=15, allow_redirects=True)
            return resp.url or url
        except Exception as exc:
            logger.debug(f"[ExternalMusic] Could not resolve short Deezer URL: {exc}")
            return url
