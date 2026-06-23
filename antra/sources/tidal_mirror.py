"""
Tidal mirror adapter — 24-bit HiRes FLAC via your self-hosted tidal_server.py.

Calls your laptop server at TIDAL_MIRROR_URL instead of the tidalapi library
directly. This avoids the India IP restriction since the server handles the
Tidal API calls through its per-session SOCKS5 proxies.

Priority 1 — highest priority, tried before all other lossless sources.
Falls back gracefully if the server is offline (returns None from search).

Config (.env):
  TIDAL_MIRROR_URL=https://your-tidal-host.example   (or http://localhost:7338)
"""
import logging
import os
import threading
import time
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.sources.odesli import OdesliEnricher
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.60
RELAY_CONNECT_TIMEOUT = 30
RELAY_READ_TIMEOUT = float(os.getenv("ANTRA_TIDAL_RELAY_READ_TIMEOUT", "240") or "240")
_SUBPROCESS_FLAGS = {}
if __import__("sys").platform == "win32":
    import subprocess as _sp
    _SUBPROCESS_FLAGS["creationflags"] = _sp.CREATE_NO_WINDOW


_quality_fallback_tls = threading.local()


class TidalMirrorAdapter(BaseSourceAdapter):
    """
    Downloads 24-bit HiRes FLAC from your self-hosted Tidal mirror server.
    Uses /api/search/isrc/{isrc} for exact matching, /api/track/{id} for streams.
    """

    name = "tidal_mirror"
    priority = 1  # Highest — 24-bit HiRes FLAC

    def __init__(self, mirror_url: str, api_key: str = "", preferred_output_format: str = "source"):
        self._base = mirror_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Antra/1.0",
            "Accept": "application/json",
        })
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        self._preferred_output_format = (preferred_output_format or "source").lower()
        self._available: Optional[bool] = None  # cached health check result
        self._album_id_cache: dict[str, str] = {}
        self._album_track_cache: dict[str, list[dict]] = {}
        self._odesli = OdesliEnricher()

    def _requires_strict_24bit(self) -> bool:
        if getattr(_quality_fallback_tls, "current_fallback", False):
            return False
        return self._preferred_output_format in {"lossless-24", "alac-24"}

    def _requires_16bit(self) -> bool:
        """True when the user explicitly wants 16-bit FLAC/ALAC. With TV-client
        Tidal sessions, requesting audioquality=LOSSLESS returns native 16-bit FLAC."""
        if getattr(_quality_fallback_tls, "current_fallback", False):
            return False
        return self._preferred_output_format in {"lossless-16", "alac-16"}

    def _stream_params(self) -> Optional[dict]:
        """Quality query params for the mirror's /api/stream and /api/track calls.
        strict_24 (24-bit) and prefer_16 (native 16-bit LOSSLESS) are mutually exclusive.
        The transcoder still downsamples as a safety net if a 24-bit file arrives in
        16-bit mode (e.g. a legacy default-client session in the pool)."""
        if self._requires_strict_24bit():
            return {"strict_24": "1"}
        if self._requires_16bit():
            return {"prefer_16": "1"}
        if getattr(_quality_fallback_tls, "current_fallback", False):
            return {"require_lossless": "1"}
        return None

    def is_available(self) -> bool:
        if not self._base:
            return False
        if self._available is not None:
            return self._available
        try:
            r = self._session.get(f"{self._base}/", timeout=15)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        return self._available

    def _reset_availability(self):
        self._available = None

    @staticmethod
    def _album_cache_key(track: TrackMetadata) -> str:
        artist = (track.primary_artist or "").strip().lower()
        album = (track.album or "").strip().lower()
        return f"{artist}::{album}"

    def _cache_album_id_from_track(self, track: TrackMetadata, track_id: str) -> None:
        cache_key = self._album_cache_key(track)
        if not cache_key or cache_key.endswith("::"):
            return
        if cache_key in self._album_id_cache:
            return
        try:
            r = self._session.get(f"{self._base}/api/meta/track/{track_id}", timeout=10)
            if r.status_code != 200:
                return
            data = r.json()
            album = data.get("album") or {}
            album_id = str(album.get("id") or "").strip()
            if album_id:
                self._album_id_cache[cache_key] = album_id
        except Exception as e:
            logger.debug("[TidalMirror] Album cache warmup failed for track %s: %s", track_id, e)

    def _search_cached_album(self, track: TrackMetadata) -> Optional[SearchResult]:
        cache_key = self._album_cache_key(track)
        album_id = self._album_id_cache.get(cache_key)
        if not album_id:
            return None

        tracks = self._album_track_cache.get(album_id)
        if tracks is None:
            try:
                r = self._session.get(f"{self._base}/api/album/{album_id}", timeout=10)
                if r.status_code != 200:
                    return None
                tracks = r.json().get("tracks") or []
                self._album_track_cache[album_id] = tracks
            except Exception as e:
                logger.debug("[TidalMirror] Album fallback lookup failed for album %s: %s", album_id, e)
                return None

        best_item: Optional[dict] = None
        best_score = 0.0
        for item in tracks:
            artists = item.get("artists") or []
            result_artist = ", ".join(artists) if isinstance(artists, list) else str(artists)
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=item.get("title", ""),
                result_artist=result_artist,
            )
            if track.track_number and item.get("track_number") == track.track_number:
                score += 0.05
            item_duration_ms = item.get("duration_ms")
            if track.duration_ms and item_duration_ms:
                if not duration_close(track.duration_ms / 1000, item_duration_ms / 1000, tolerance=30):
                    continue
            if score > best_score:
                best_score = score
                best_item = item

        if not best_item or best_score < 0.72:
            return None

        return SearchResult(
            source=self.name,
            title=best_item.get("title", ""),
            artists=best_item.get("artists") or [],
            album=track.album,
            duration_ms=best_item.get("duration_ms"),
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=None,
            sample_rate_hz=None,
            download_url=None,
            stream_id=str(best_item["track_id"]),
            similarity_score=1.0,
            isrc_match=True,
            is_explicit=best_item.get("explicit") if isinstance(best_item.get("explicit"), bool) else None,
        )

    @staticmethod
    def _text_search_queries(track: TrackMetadata) -> list[str]:
        queries = [f"{track.title} {track.primary_artist}".strip()]
        album = (track.album or "").strip()
        if album:
            augmented = f"{track.title} {track.primary_artist} {album}".strip()
            if augmented.lower() != queries[0].lower():
                queries.append(augmented)
        return queries

    @staticmethod
    def _has_severe_duration_mismatch(
        expected_duration_ms: Optional[int],
        candidate_duration_ms: Optional[int],
    ) -> bool:
        if not expected_duration_ms or not candidate_duration_ms:
            return False
        expected_s = expected_duration_ms / 1000.0
        candidate_s = candidate_duration_ms / 1000.0
        shorter = (
            candidate_s < expected_s * 0.8
            and (expected_s - candidate_s) >= 20
        )
        longer = (
            candidate_s > expected_s * 1.3
            and (candidate_s - expected_s) >= 45
        )
        return shorter or longer

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        # Direct track-ID shortcut — when the source URL was a Tidal URL we already
        # know the exact Tidal track ID; skip ISRC / text search entirely.
        if track.tidal_track_id:
            logger.info("[TidalMirror] Using known Tidal track ID %s for '%s'", track.tidal_track_id, track.title)
            sm = {"isrc": track.isrc} if track.isrc else {}
            return SearchResult(
                source=self.name,
                title=track.title,
                artists=track.artists,
                album=track.album,
                duration_ms=track.duration_ms,
                audio_format=AudioFormat.FLAC,
                quality_kbps=None,
                is_lossless=True,
                bit_depth=None,
                sample_rate_hz=None,
                download_url=None,
                stream_id=track.tidal_track_id,
                similarity_score=1.0,
                isrc_match=bool(track.isrc),
                is_explicit=None,
                source_metadata=sm,
            )

        # Spotify links can fail over poorly after a Qobuz stream error because
        # they may have no ISRC and Tidal text search is much weaker than an
        # exact cross-platform mapping. Odesli/Songwhip already cache these IDs,
        # so use them before text search.
        if track.spotify_id or track.isrc:
            try:
                platform_ids = self._odesli.resolve(track)
                tidal_id = platform_ids.get("tidal")
                if tidal_id:
                    logger.info("[TidalMirror] Odesli resolved Tidal track ID %s for '%s'", tidal_id, track.title)
                    sm = {"isrc": track.isrc} if track.isrc else {}
                    return SearchResult(
                        source=self.name,
                        title=track.title,
                        artists=track.artists,
                        album=track.album,
                        duration_ms=track.duration_ms,
                        audio_format=AudioFormat.FLAC,
                        quality_kbps=None,
                        is_lossless=True,
                        bit_depth=None,
                        sample_rate_hz=None,
                        download_url=None,
                        stream_id=str(tidal_id),
                        similarity_score=1.0,
                        isrc_match=bool(track.isrc),
                        is_explicit=track.is_explicit,
                        source_metadata=sm,
                    )
            except Exception as e:
                logger.debug("[TidalMirror] Odesli Tidal ID lookup failed: %s", e)

        # ISRC exact match via mirror's search endpoint
        if track.isrc:
            try:
                r = self._session.get(
                    f"{self._base}/api/search/isrc/{track.isrc}",
                    timeout=10,
                )
                if r.status_code == 429:
                    raise RateLimitedError("Tidal mirror rate limited (429)")
                if r.status_code in (401, 403):
                    # Key invalid — log once and return None (don't disable permanently)
                    logger.warning("[TidalMirror] API key rejected (%d) — check key on server", r.status_code)
                    return None
                if r.status_code == 503:
                    self._reset_availability()
                    return None
                if r.status_code == 200:
                    data = r.json()
                    result = self._build_result(data, isrc_match=True)
                    if self._has_severe_duration_mismatch(track.duration_ms, result.duration_ms):
                        logger.info(
                            "[TidalMirror] ISRC match for '%s' rejected — severe duration mismatch "
                            "(expected %.0fs, got %.0fs)",
                            track.title,
                            (track.duration_ms or 0) / 1000,
                            (result.duration_ms or 0) / 1000,
                        )
                        result = None
                    # Sanity-check duration: if the source track is significantly
                    # shorter or longer than expected, the ISRC lookup returned the
                    # wrong recording (e.g. Pt. 1 and Pt. 2 mapped to the same track).
                    if track.duration_ms and result.duration_ms:
                        if not duration_close(
                            track.duration_ms / 1000,
                            result.duration_ms / 1000,
                            tolerance=30,
                        ):
                            logger.info(
                                "[TidalMirror] ISRC match for '%s' rejected — "
                                "duration mismatch (expected %.0fs, got %.0fs)",
                                track.title,
                                track.duration_ms / 1000,
                                result.duration_ms / 1000,
                            )
                            result = None
                    if result is not None:
                        self._cache_album_id_from_track(track, str(data.get("track_id") or result.stream_id or ""))
                        return result
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[TidalMirror] ISRC search failed: %s", e)

        cached_album_result = self._search_cached_album(track)
        if cached_album_result is not None:
            return cached_album_result

        best: Optional[SearchResult] = None
        best_score = 0.0
        for query in self._text_search_queries(track):
            try:
                r = self._session.get(
                    f"{self._base}/search/",
                    params={"s": query},
                    timeout=10,
                )
                if r.status_code == 429:
                    raise RateLimitedError("Tidal mirror rate limited (429)")
                if r.status_code in (401, 403):
                    logger.warning("[TidalMirror] API key rejected (%d) — check key on server", r.status_code)
                    return None
                if r.status_code != 200:
                    continue
                items = r.json().get("data", {}).get("items", [])
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[TidalMirror] Text search failed: %s", e)
                continue

            for item in items:
                artist = (item.get("artist") or {}).get("name", "")
                score = score_similarity(
                    query_title=track.title,
                    query_artists=track.artists,
                    result_title=item.get("title", ""),
                    result_artist=artist,
                )
                dur = item.get("duration")
                if dur and track.duration_seconds:
                    if self._has_severe_duration_mismatch(track.duration_ms, int(dur * 1000)):
                        continue
                    if not duration_close(track.duration_seconds, dur, tolerance=5):
                        score *= 0.8
                if score > best_score:
                    best_score = score
                    best = self._item_to_result(item, score)

        if best and best_score >= MIN_SIMILARITY:
            return best
        return None

    def _build_result(self, data: dict, isrc_match: bool = False) -> SearchResult:
        """Build SearchResult from ISRC search response."""
        # Read actual quality from server response — don't assume 24-bit
        bit_depth = data.get("bitDepth")
        sample_rate = data.get("sampleRate")
        # If server didn't return quality info, leave as None so resolver
        # doesn't over-report quality
        return SearchResult(
            source=self.name,
            title=data.get("title", ""),
            artists=[data.get("artist", "")],
            album=data.get("album", ""),
            duration_ms=data.get("duration_ms"),
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=bit_depth,
            sample_rate_hz=sample_rate,
            download_url=None,
            stream_id=str(data["track_id"]),
            similarity_score=1.0 if isrc_match else 0.85,
            isrc_match=isrc_match,
            is_explicit=data.get("explicit") if isinstance(data.get("explicit"), bool) else None,
            source_metadata=_extract_tidal_source_meta(data),
        )

    def _item_to_result(self, item: dict, score: float) -> SearchResult:
        """Build SearchResult from HiFi-compatible search item."""
        artist = (item.get("artist") or {}).get("name", "")
        quality = str(item.get("audioQuality", "")).upper()
        tags = [str(t).upper() for t in (item.get("mediaMetadata") or {}).get("tags", [])]
        blob = " ".join([quality] + tags)
        # Use None for unknown bit_depth — the actual stream may be higher quality
        # than what the search metadata reports (Tidal often returns LOSSLESS for
        # tracks that stream as HI_RES_LOSSLESS). The real quality is in the file.
        if "HI_RES" in blob or "HIRES" in blob:
            bit_depth = 24
            sample_rate = 96000
        elif "LOSSLESS" in blob:
            # Don't assume 16-bit — Tidal serves many LOSSLESS-tagged tracks as 24-bit
            bit_depth = None
            sample_rate = None
        else:
            bit_depth = None
            sample_rate = None
        return SearchResult(
            source=self.name,
            title=item.get("title", ""),
            artists=[artist] if artist else [],
            album=(item.get("album") or {}).get("title"),
            duration_ms=int(item["duration"] * 1000) if item.get("duration") else None,
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=bit_depth,
            sample_rate_hz=sample_rate,
            download_url=None,
            stream_id=str(item["id"]),
            similarity_score=score,
            isrc_match=False,
            is_explicit=item.get("explicit") if isinstance(item.get("explicit"), bool) else None,
            source_metadata=_extract_tidal_source_meta(item),
        )

    def download(self, result: SearchResult, output_path: str) -> str:
        _quality_fallback_tls.current_fallback = getattr(_quality_fallback_tls, "accept_lossless", False)
        if _quality_fallback_tls.current_fallback:
            _quality_fallback_tls.accept_lossless = False
        track_id = result.stream_id

        # Primary path: server-side relay — VPS fetches from Tidal CDN with its
        # EU/US IP and streams bytes to us, bypassing India geo-blocks entirely.
        # Falls back to direct CDN download if the relay endpoint is absent
        # (old server version that predates VPS migration).
        try:
            r = self._session.get(
                f"{self._base}/api/stream/{track_id}",
                params=self._stream_params(),
                stream=True,
                timeout=(RELAY_CONNECT_TIMEOUT, RELAY_READ_TIMEOUT),
            )
            if r.status_code == 429:
                raise RateLimitedError("Tidal mirror rate limited (429)")
            if r.status_code in (502, 503):
                self._reset_availability()
                raise RuntimeError(f"[TidalMirror] Server unavailable ({r.status_code})")
            if r.status_code == 409:
                # Server rejected: quality unavailable (strict 24-bit) or no healthy sessions.
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:
                    pass
                if self._requires_strict_24bit():
                    raise RuntimeError(f"[TidalMirror] Quality mismatch (strict 24-bit): {detail or track_id}")
                else:
                    raise RuntimeError(f"[TidalMirror] Quality mismatch (lossless unavailable): {detail or track_id}")
            if r.status_code == 404:
                raise RuntimeError(f"[TidalMirror] Track {track_id} not found on Tidal")
            if r.status_code == 200:
                stream_quality = r.headers.get("X-Quality", "").upper()
                stream_codec = r.headers.get("X-Codec", "").lower()
                # Reject HIGH (lossy AAC). Do NOT hunt for an alternate track ID via
                # ISRC — that frequently resolves to a different release/version whose
                # sessions are unhealthy (the AAC-only sibling), which then 409s with
                # "No healthy Tidal sessions available for track <other-id>". Just raise;
                # should_retry_download() retries the SAME known track_id so the server
                # picks a different proven-lossless session. This matches Antra-Web,
                # which is exactly why the website succeeds where desktop currently fails.
                if stream_quality == "HIGH":
                    r.close()
                    raise RuntimeError(
                        f"[TidalMirror] Track {track_id} returned HIGH (lossy AAC) — retrying"
                    )
                # Reject AAC-in-disguise (codec mp4a/aac despite a LOSSLESS label) so we
                # don't remux a fake-lossless AAC master into a FLAC container.
                if "mp4a" in stream_codec or ("aac" in stream_codec and "flac" not in stream_codec):
                    r.close()
                    raise RuntimeError(
                        f"[TidalMirror] Track {track_id} codec is AAC despite LOSSLESS label — "
                        "rejecting fake lossless, falling back to next source"
                    )
                if self._requires_strict_24bit() and stream_quality and stream_quality != "HI_RES_LOSSLESS":
                    r.close()
                    raise RuntimeError(
                        f"[TidalMirror] Quality mismatch (strict 24-bit): "
                        f"got {stream_quality}, need HI_RES_LOSSLESS"
                    )
                codec = r.headers.get("X-Codec", "flac").lower()
                ext = ".flac" if "flac" in codec else ".m4a"
                final_path = output_path + ext
                os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
                total_bytes = 0
                content_length = 0
                cl = r.headers.get("Content-Length")
                if cl:
                    content_length = int(cl)
                with open(final_path, "wb") as f:
                    for chunk in r.iter_content(131072):
                        if chunk:
                            total_bytes += len(chunk)
                            f.write(chunk)
                # Verify downloaded bytes are reasonable for the track duration.
                # This catches server-relay truncation where the CDN drops mid-stream.
                if content_length > 0 and total_bytes < content_length * 0.90:
                    try:
                        os.remove(final_path)
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"[TidalMirror] Truncated download: got {total_bytes}/{content_length} bytes "
                        f"({total_bytes * 100 // max(content_length, 1)}%) — "
                        "CDN stream was cut short, retrying"
                    )
                # StreamingResponse from the VPS relay has no Content-Length header,
                # so the byte-count check above is always skipped. Use ffprobe to
                # verify actual audio duration and catch 30-second CDN preview clips.
                self._check_preview_clip(final_path, result.duration_ms)
                # Tidal wraps FLAC in an M4A container (ISO Base Media / ftyp)
                # even when the codec header says "flac". Detect by magic bytes
                # and remux to a real FLAC file regardless of the reported extension.
                if self._is_m4a_by_magic(final_path):
                    import tempfile, shutil
                    tmp = final_path + ".tmp.m4a"
                    shutil.move(final_path, tmp)
                    try:
                        if self._remux_m4a_to_flac(tmp, output_path + ".flac"):
                            os.remove(tmp)
                            return output_path + ".flac"
                        else:
                            # Remux failed — restore original and return as-is
                            shutil.move(tmp, final_path)
                    except Exception:
                        # Clean up temp file on any unexpected error
                        if os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                        raise
                elif ext == ".m4a":
                    flac_path = output_path + ".flac"
                    if self._remux_m4a_to_flac(final_path, flac_path):
                        os.remove(final_path)
                        return flac_path
                return final_path
            # Any other status (e.g. 501 Not Implemented on old server): fall through
            logger.debug("[TidalMirror] Relay returned %d — falling back to direct CDN", r.status_code)
        except RateLimitedError:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            logger.debug("[TidalMirror] Relay unavailable (%s) — falling back to direct CDN", e)

        # Fallback: get CDN URL from server, download directly (pre-VPS behaviour)
        try:
            r = self._session.get(
                f"{self._base}/api/track/{track_id}",
                params=self._stream_params(),
                timeout=30,
            )
            if r.status_code == 429:
                raise RateLimitedError("Tidal mirror rate limited (429)")
            if r.status_code == 503:
                self._reset_availability()
                raise RuntimeError("[TidalMirror] Server unavailable (503)")
            if r.status_code == 409:
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:
                    pass
                if self._requires_strict_24bit():
                    raise RuntimeError(f"[TidalMirror] Quality mismatch (strict 24-bit): {detail or track_id}")
                else:
                    raise RuntimeError(f"[TidalMirror] Quality mismatch (lossless unavailable): {detail or track_id}")
            r.raise_for_status()
            data = r.json()
        except RateLimitedError:
            raise
        except Exception as e:
            raise RuntimeError(f"[TidalMirror] Track request failed: {e}") from e

        stream_url = data.get("streamUrl")
        if not stream_url:
            raise RuntimeError(f"[TidalMirror] No streamUrl in response for track {track_id}")

        codec = (data.get("codec") or "flac").lower()
        ext = ".flac" if "flac" in codec else ".m4a"
        urls = data.get("streamUrls") or [stream_url]

        if len(urls) > 1:
            path = self._download_segments(urls, output_path, ext)
        else:
            path = self._download_single(stream_url, output_path, ext)
        self._check_preview_clip(path, result.duration_ms)
        return path

    def _download_single(self, url: str, output_base: str, ext: str) -> str:
        final_path = output_base + ext
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
        # Use a longer connect timeout but no read timeout — Tidal CDN URLs
        # expire in ~5 min but the download itself can take time on slow connections.
        with self._session.get(url, stream=True, timeout=(15, 60)) as r:
            r.raise_for_status()
            with open(final_path, "wb") as f:
                for chunk in r.iter_content(131072):  # 128KB chunks
                    if chunk:
                        f.write(chunk)
        # Remux M4A→FLAC if needed (Tidal wraps FLAC in M4A container)
        if ext == ".m4a":
            flac_path = output_base + ".flac"
            if self._remux_m4a_to_flac(final_path, flac_path):
                os.remove(final_path)
                return flac_path
        return final_path

    def _download_segments(self, urls: list[str], output_base: str, ext: str) -> str:
        import tempfile, shutil, subprocess
        from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env
        tmp_dir = tempfile.mkdtemp(prefix="antra_tidal_mirror_")
        segs = []
        try:
            for i, url in enumerate(urls):
                seg = os.path.join(tmp_dir, f"seg_{i:05d}.part")
                # No read timeout — segments can be large, connect timeout 15s
                with self._session.get(url, stream=True, timeout=(15, 60)) as r:
                    r.raise_for_status()
                    with open(seg, "wb") as f:
                        for chunk in r.iter_content(131072):
                            if chunk:
                                f.write(chunk)
                segs.append(seg)
            final_path = output_base + ext
            os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
            # Try ffmpeg concat
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            tmp_list = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
            for seg in segs:
                tmp_list.write(f"file '{seg}'\n")
            tmp_list.close()
            result = subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", tmp_list.name, "-c", "copy", final_path],
                capture_output=True, timeout=180, env=get_clean_subprocess_env(), **_SUBPROCESS_FLAGS,
            )
            os.unlink(tmp_list.name)
            if result.returncode != 0:
                # Raw concat fallback
                with open(final_path, "wb") as out:
                    for seg in segs:
                        with open(seg, "rb") as f:
                            out.write(f.read())
            if ext == ".m4a":
                flac_path = output_base + ".flac"
                if self._remux_m4a_to_flac(final_path, flac_path):
                    os.remove(final_path)
                    return flac_path
            return final_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        """Don't retry definitive failures — only transient ones."""
        msg = str(error).lower()
        # Hard 404 — the track genuinely does not exist on this mirror; retrying won't help.
        if "not found on tidal" in msg or "no streamurl" in msg:
            return False
        if "duration mismatch" in msg:
            return False
        # Preview clip from CDN — server already tried up to 4 sessions internally.
        # Don't retry tidal_mirror further; let the engine fall to Qobuz/Deezer.
        if "preview clip detected" in msg or "truncated stream" in msg:
            return False
        # HIGH quality (lossy AAC) is session-dependent — retry so engine calls us
        # again and server picks a different session that may return HI_RES_LOSSLESS.
        if "returned high (lossy aac)" in msg:
            return True
        # Quality mismatch — catalog-level or session-exhausted; don't retry.
        if "quality mismatch (strict 24-bit)" in msg or "quality mismatch (lossless unavailable)" in msg:
            return False
        # Server unavailable (502/503) — could come back, but retrying immediately won't help;
        # let the outer loop try the next source instead.
        if "server unavailable" in msg or "502" in msg or "503" in msg:
            return False
        return True

    def should_exclude_adapter_after_failure(self, result, error) -> bool:
        msg = str(error).lower()
        if "quality mismatch (strict 24-bit)" in msg:
            # Session exhaustion or genuinely AAC-only track in strict 24-bit mode.
            if ("unconfirmable" in msg or "no healthy" in msg
                    or "only available as high" in msg or "returned lossy" in msg):
                # All sessions exhausted — fall through to Qobuz/Deezer immediately.
                return True
            # Track exists at 16-bit LOSSLESS but not HI_RES_LOSSLESS (STRICT_24_QUALITY_MISMATCH).
            # Set accept_lossless so the next attempt skips strict_24 and accepts 16-bit LOSSLESS.
            _quality_fallback_tls.accept_lossless = True
            return False
        if "quality mismatch (lossless unavailable)" in msg:
            # 16-bit mode: server couldn't serve LOSSLESS (sessions exhausted or track is AAC-only).
            # Exclude this result and fall through to Qobuz/Deezer.
            return True
        return True

    @staticmethod
    def _ffprobe_duration(file_path: str) -> Optional[float]:
        """Return audio duration in seconds via ffprobe, or None if unavailable."""
        try:
            import subprocess
            from antra.utils.runtime import get_ffprobe_exe, get_clean_subprocess_env
            ffprobe = get_ffprobe_exe() or "ffprobe"
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", file_path],
                capture_output=True, text=True, timeout=15,
                env=get_clean_subprocess_env(), **_SUBPROCESS_FLAGS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    def _check_preview_clip(self, file_path: str, expected_duration_ms: Optional[int]) -> None:
        """Raise RuntimeError if downloaded file appears to be a CDN preview clip (≤30s)."""
        if not expected_duration_ms or expected_duration_ms <= 60_000:
            return  # track is too short to reliably distinguish from a preview
        actual_s = self._ffprobe_duration(file_path)
        if actual_s is None:
            return  # ffprobe unavailable — skip check
        expected_s = expected_duration_ms / 1000
        if actual_s < expected_s * 0.8 and (expected_s - actual_s) > 30:
            try:
                os.remove(file_path)
            except OSError:
                pass
            raise RuntimeError(
                f"[TidalMirror] Preview clip detected: "
                f"got {actual_s:.0f}s, expected {expected_s:.0f}s "
                "— CDN served truncated stream"
            )

    @staticmethod
    def _is_m4a_by_magic(file_path: str) -> bool:
        """Return True if the file is an M4A/MP4 container (ISO Base Media ftyp box)."""
        try:
            with open(file_path, "rb") as f:
                header = f.read(12)
            # M4A/MP4: bytes 4-7 are b"ftyp"
            return len(header) >= 8 and header[4:8] == b"ftyp"
        except Exception:
            return False

    @staticmethod
    def _remux_m4a_to_flac(input_path: str, output_path: str) -> bool:
        try:
            import subprocess
            from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env
            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            result = subprocess.run(
                [ffmpeg, "-y", "-i", input_path, "-c", "copy", "-f", "flac", output_path],
                capture_output=True, timeout=120,
                env=get_clean_subprocess_env(), **_SUBPROCESS_FLAGS,
            )
            return result.returncode == 0
        except Exception:
            return False


def _extract_tidal_source_meta(data: dict) -> dict:
    """Extract metadata from a Tidal API response dict."""
    meta: dict = {}
    isrc = (data.get("isrc") or "").strip()
    if isrc:
        meta["isrc"] = isrc
    if isinstance(data.get("explicit"), bool):
        meta["is_explicit"] = data["explicit"]
    tn = data.get("track_number") or data.get("trackNumber")
    if tn is not None:
        try:
            meta["track_number"] = int(tn)
        except (TypeError, ValueError):
            pass
    dn = data.get("disc_number") or data.get("volumeNumber") or data.get("discNumber")
    if dn is not None:
        try:
            meta["disc_number"] = int(dn)
        except (TypeError, ValueError):
            pass
    art = (data.get("album") or {}).get("cover") or (data.get("album") or {}).get("imageCoverUrl")
    if art:
        meta["artwork_url"] = art
    rd = data.get("release_date") or (data.get("album") or {}).get("releaseDate") or ""
    if rd:
        meta["release_date"] = rd[:10] if len(rd) >= 10 else rd
    return meta
