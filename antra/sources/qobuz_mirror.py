"""
Qobuz mirror adapter — 24-bit FLAC via your self-hosted qobuz_server.py.

Calls your laptop server at QOBUZ_MIRROR_URL.
Priority 1 — same tier as Tidal mirror, rotated for load distribution.

Config (.env):
  QOBUZ_MIRROR_URL=https://your-qobuz-host.example   (or http://localhost:7343)
"""
import logging
import os
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.60


class QobuzMirrorAdapter(BaseSourceAdapter):
    """
    Downloads 24-bit FLAC from your self-hosted Qobuz mirror server.
    """

    name = "qobuz_mirror"
    priority = 1  # Highest — 24-bit FLAC

    def __init__(self, mirror_url: str, api_key: str = ""):
        self._base = mirror_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Antra/1.0", "Accept": "application/json"})
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if not self._base:
            return False
        if self._available is not None:
            return self._available
        try:
            r = self._session.get(f"{self._base}/", timeout=5)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        return self._available

    def _reset_availability(self):
        self._available = None

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        if track.isrc:
            try:
                r = self._session.get(
                    f"{self._base}/api/search/isrc/{track.isrc}",
                    timeout=10,
                )
                if r.status_code == 429:
                    raise RateLimitedError("Qobuz mirror rate limited (429)")
                if r.status_code in (401, 403):
                    logger.warning("[QobuzMirror] API key rejected (%d) — check key on server", r.status_code)
                    return None
                if r.status_code == 503:
                    self._reset_availability()
                    return None
                if r.status_code == 200:
                    data = r.json()
                    result_duration_ms = data.get("duration_ms")
                    # Sanity-check duration: if the source track is significantly
                    # shorter or longer than expected, the ISRC lookup returned the
                    # wrong recording (e.g. Pt. 1 and Pt. 2 mapped to the same track).
                    if track.duration_ms and result_duration_ms:
                        if not duration_close(
                            track.duration_ms / 1000,
                            result_duration_ms / 1000,
                            tolerance=30,
                        ):
                            logger.info(
                                "[QobuzMirror] ISRC match for '%s' rejected — "
                                "duration mismatch (expected %.0fs, got %.0fs)",
                                track.title,
                                track.duration_ms / 1000,
                                result_duration_ms / 1000,
                            )
                            return self._text_search(track)
                    return SearchResult(
                        source=self.name,
                        title=data.get("title", ""),
                        artists=[data.get("artist", "")],
                        album=data.get("album", ""),
                        duration_ms=result_duration_ms,
                        audio_format=AudioFormat.FLAC,
                        quality_kbps=None,
                        is_lossless=True,
                        bit_depth=data.get("bitDepth") or 24,
                        sample_rate_hz=data.get("sampleRate") or 44100,
                        download_url=None,
                        stream_id=str(data["track_id"]),
                        similarity_score=1.0,
                        isrc_match=True,
                        is_explicit=data.get("explicit") if isinstance(data.get("explicit"), bool) else None,
                    )
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[QobuzMirror] ISRC search failed: %s", e)

        # Text search fallback — used when ISRC is unavailable
        return self._text_search(track)

    def _text_search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            r = self._session.get(
                f"{self._base}/api/search",
                params={"title": track.title, "artist": track.primary_artist, "limit": 5},
                timeout=10,
            )
            if r.status_code == 429:
                raise RateLimitedError("Qobuz mirror rate limited (429)")
            if r.status_code in (401, 403):
                logger.warning("[QobuzMirror] API key rejected (%d) — check key on server", r.status_code)
                return None
            if r.status_code != 200:
                return None
            items = r.json().get("results") or []
        except RateLimitedError:
            raise
        except Exception as e:
            logger.debug("[QobuzMirror] Text search failed: %s", e)
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0
        for item in items:
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=item.get("title", ""),
                result_artist=item.get("artist", ""),
            )
            dur_ms = item.get("duration_ms")
            if dur_ms and track.duration_ms:
                if not duration_close(track.duration_ms / 1000, dur_ms / 1000, tolerance=5):
                    score *= 0.8
            if score > best_score:
                best_score = score
                bit_depth = item.get("bitDepth") or 24
                best = SearchResult(
                    source=self.name,
                    title=item.get("title", ""),
                    artists=[item.get("artist", "")],
                    album=item.get("album", ""),
                    duration_ms=dur_ms,
                    audio_format=AudioFormat.FLAC,
                    quality_kbps=None,
                    is_lossless=True,
                    bit_depth=bit_depth,
                    sample_rate_hz=item.get("sampleRate") or (44100 if bit_depth < 24 else 96000),
                    download_url=None,
                    stream_id=str(item["track_id"]),
                    similarity_score=score,
                    isrc_match=False,
                    is_explicit=item.get("explicit") if isinstance(item.get("explicit"), bool) else None,
                )

        if best and best_score >= MIN_SIMILARITY:
            return best
        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        track_id = result.stream_id
        # Use /api/stream/ endpoint — server fetches CDN URL and pipes bytes directly.
        # This avoids Qobuz CDN URL expiry (URLs expire in ~2-3 min; retries would fail).
        try:
            r = self._session.get(
                f"{self._base}/api/stream/{track_id}",
                stream=True,
                timeout=(15, None),  # 15s connect, no read timeout
            )
            if r.status_code == 429:
                raise RateLimitedError("Qobuz mirror rate limited (429)")
            if r.status_code in (401, 403):
                logger.warning("[QobuzMirror] API key rejected (%d) — check key on server", r.status_code)
                raise RuntimeError("[QobuzMirror] API key rejected")
            if r.status_code == 503:
                self._reset_availability()
                raise RuntimeError("[QobuzMirror] Server unavailable (503)")
            r.raise_for_status()
        except RateLimitedError:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"[QobuzMirror] Stream request failed: {e}") from e

        # Read quality from response headers set by the server
        bit_depth_hdr = r.headers.get("X-Qobuz-BitDepth", "")
        quality_hdr   = r.headers.get("X-Qobuz-Quality", "")

        # Detect extension from Content-Type
        ct = r.headers.get("Content-Type", "audio/flac").lower()
        ext = ".flac" if "flac" in ct else ".m4a"

        final_path = output_path + ext
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
        with open(final_path, "wb") as f:
            for chunk in r.iter_content(131072):
                if chunk:
                    f.write(chunk)

        logger.info("[QobuzMirror] Downloaded %s quality=%s bit_depth=%s",
                    os.path.basename(final_path), quality_hdr, bit_depth_hdr)
        return final_path
