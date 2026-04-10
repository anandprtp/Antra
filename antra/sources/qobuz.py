"""
Qobuz source adapter — preferred for lossless FLAC downloads.
Requires Qobuz account credentials (email + password or app_id + app_secret).

Qobuz has no official public API; this uses the internal streaming API
endpoints that are also used by tools like streamrip and orpheus-dl.
"""
import hashlib
import logging
import os
import time
from typing import Optional

import requests

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.80

# Legacy static app_id — no longer valid; kept only as a sentinel to detect
# when the user has not configured a real one. The adapter will auto-fetch
# fresh credentials via Playwright when this value is detected.
_STALE_APP_ID = "285473059"


class QobuzAdapter(BaseSourceAdapter):
    name = "qobuz"
    priority = 10  # Highest priority

    BASE_URL = "https://www.qobuz.com/api.json/0.2"

    def __init__(self, email: str = "", password: str = "", app_id: str = "", app_secret: str = "", user_auth_token: str = ""):
        self.email = email
        self.password = password
        self.app_id = app_id or _STALE_APP_ID
        self.app_secret = app_secret
        self._user_auth_token: Optional[str] = user_auth_token or None
        self._creds_refreshed = False  # guard: only auto-refresh once per session
        self._session = requests.Session()
        self._session.headers.update({
            "X-App-Id": self.app_id,
            "User-Agent": "Mozilla/5.0",
        })

    def is_available(self) -> bool:
        return bool((self.email and self.password) or self._user_auth_token)

    def _refresh_credentials(self) -> bool:
        """Fetch fresh credentials if supported by the environment. Currently disabled for public versions."""
        return False

    def _login(self):
        """Authenticate and store user auth token."""
        if self._user_auth_token:
            if "X-User-Auth-Token" not in self._session.headers:
                self._session.headers["X-User-Auth-Token"] = self._user_auth_token
            return
        resp = self._session.post(
            f"{self.BASE_URL}/user/login",
            params={"email": self.email, "password": self.password, "app_id": self.app_id},
        )
        if resp.status_code in (401, 400) and not self._creds_refreshed:
            # App ID is likely stale — refresh and retry once
            logger.info(f"[Qobuz] Login returned {resp.status_code} — app_id may be revoked, refreshing…")
            if self._refresh_credentials():
                resp = self._session.post(
                    f"{self.BASE_URL}/user/login",
                    params={"email": self.email, "password": self.password, "app_id": self.app_id},
                )
        resp.raise_for_status()
        data = resp.json()
        self._user_auth_token = data["user_auth_token"]
        self._session.headers["X-User-Auth-Token"] = self._user_auth_token
        logger.info("[Qobuz] Logged in successfully.")

    def _api_get(self, endpoint: str, **kwargs):
        """Wrapper for GET requests that automatically catches stale credentials."""
        resp = self._session.get(f"{self.BASE_URL}/{endpoint}", **kwargs)
        if resp.status_code in (401, 400) and not self._creds_refreshed:
            logger.debug(f"[Qobuz] API returned {resp.status_code} — credentials may be stale, refreshing…")
            if self._refresh_credentials():
                resp = self._session.get(f"{self.BASE_URL}/{endpoint}", **kwargs)
        resp.raise_for_status()
        return resp

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            self._login()
        except Exception as e:
            logger.warning(f"[Qobuz] Login failed: {e}")
            return None

        # Try ISRC first
        if track.isrc:
            result = self._search_by_isrc(track)
            if result:
                return result

        # Fallback to text search
        return self._search_by_text(track)

    def _search_by_isrc(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            resp = self._api_get(
                "track/search",
                params={"query": track.isrc, "limit": 5},
            )
            items = resp.json().get("tracks", {}).get("items", [])
            for item in items:
                if item.get("isrc", "").upper() == (track.isrc or "").upper():
                    return self._item_to_result(item, track, isrc_match=True)
        except Exception as e:
            logger.debug(f"[Qobuz] ISRC search failed: {e}")
        return None

    def _search_by_text(self, track: TrackMetadata) -> Optional[SearchResult]:
        query = f"{track.title} {track.primary_artist}"
        try:
            resp = self._api_get(
                "track/search",
                params={"query": query, "limit": 10},
            )
            items = resp.json().get("tracks", {}).get("items", [])
        except Exception as e:
            logger.warning(f"[Qobuz] Text search failed for '{query}': {e}")
            return None

        best = None
        best_score = 0.0

        for item in items:
            performer = item.get("performer", {}).get("name", "")
            title = item.get("title", "")
            duration = item.get("duration")

            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=title,
                result_artist=performer,
            )

            if duration and track.duration_seconds:
                if not duration_close(track.duration_seconds, duration, tolerance=5):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = self._item_to_result(item, track)
                if best:
                    best.similarity_score = score

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[Qobuz] Match score={best_score:.2f}: {best.title}")
            return best

        return None

    def _item_to_result(self, item: dict, track: TrackMetadata, isrc_match: bool = False) -> Optional[SearchResult]:
        try:
            performer = item.get("performer", {}).get("name", item.get("album", {}).get("artist", {}).get("name", ""))
            duration_s = item.get("duration")
            return SearchResult(
                source=self.name,
                title=item.get("title", ""),
                artists=[performer],
                album=item.get("album", {}).get("title"),
                duration_ms=int(duration_s * 1000) if duration_s else None,
                audio_format=AudioFormat.FLAC,
                quality_kbps=None,
                is_lossless=True,
                download_url=None,
                stream_id=str(item["id"]),
                similarity_score=1.0 if isrc_match else 0.0,
                isrc_match=isrc_match,
            )
        except Exception:
            return None

    def download(self, result: SearchResult, output_path: str) -> str:
        """Fetch a time-limited streaming URL and download the FLAC."""
        self._login()
        track_id = result.stream_id
        format_id = 27  # 27 = FLAC 24-bit, 6 = FLAC 16-bit, 5 = MP3 320

        # Build request signature
        ts = str(int(time.time()))
        r_sig = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{self.app_secret}"
        r_sig_hashed = hashlib.md5(r_sig.encode()).hexdigest()

        try:
            resp = self._api_get(
                "track/getFileUrl",
                params={
                    "request_ts": ts,
                    "request_sig": r_sig_hashed,
                    "track_id": track_id,
                    "format_id": format_id,
                    "intent": "stream",
                },
            )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                # Fallback to 16-bit FLAC if 24-bit was strictly not available (different to credential 400!)
                format_id = 6
                ts = str(int(time.time()))
                r_sig = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{ts}{self.app_secret}"
                r_sig_hashed = hashlib.md5(r_sig.encode()).hexdigest()
                resp = self._api_get(
                    "track/getFileUrl",
                    params={
                        "request_ts": ts,
                        "request_sig": r_sig_hashed,
                        "track_id": track_id,
                        "format_id": format_id,
                        "intent": "stream",
                    },
                )
            else:
                raise

        stream_url = resp.json().get("url")
        if not stream_url:
            raise ValueError("Qobuz returned no stream URL")

        final_path = output_path + ".flac"
        self._stream_to_file(stream_url, final_path)
        return final_path

    def _stream_to_file(self, url: str, path: str):
        with self._session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
