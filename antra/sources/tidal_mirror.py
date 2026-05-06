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
import time
from typing import Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.60
_SUBPROCESS_FLAGS = {}
if __import__("sys").platform == "win32":
    import subprocess as _sp
    _SUBPROCESS_FLAGS["creationflags"] = _sp.CREATE_NO_WINDOW


class TidalMirrorAdapter(BaseSourceAdapter):
    """
    Downloads 24-bit HiRes FLAC from your self-hosted Tidal mirror server.
    Uses /api/search/isrc/{isrc} for exact matching, /api/track/{id} for streams.
    """

    name = "tidal_mirror"
    priority = 1  # Highest — 24-bit HiRes FLAC

    def __init__(self, mirror_url: str, api_key: str = ""):
        self._base = mirror_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Antra/1.0",
            "Accept": "application/json",
        })
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        self._available: Optional[bool] = None  # cached health check result

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
                        return result
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[TidalMirror] ISRC search failed: %s", e)

        # Text search via HiFi-compatible /search/ endpoint
        query = f"{track.title} {track.primary_artist}".strip()
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
                return None
            items = r.json().get("data", {}).get("items", [])
        except RateLimitedError:
            raise
        except Exception as e:
            logger.debug("[TidalMirror] Text search failed: %s", e)
            return None

        best: Optional[SearchResult] = None
        best_score = 0.0
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
        )

    def download(self, result: SearchResult, output_path: str) -> str:
        track_id = result.stream_id

        # Primary path: server-side relay — VPS fetches from Tidal CDN with its
        # EU/US IP and streams bytes to us, bypassing India geo-blocks entirely.
        # Falls back to direct CDN download if the relay endpoint is absent
        # (old server version that predates VPS migration).
        try:
            r = self._session.get(
                f"{self._base}/api/stream/{track_id}",
                stream=True,
                timeout=(30, None),
            )
            if r.status_code == 429:
                raise RateLimitedError("Tidal mirror rate limited (429)")
            if r.status_code in (502, 503):
                self._reset_availability()
                raise RuntimeError(f"[TidalMirror] Server unavailable ({r.status_code})")
            if r.status_code == 404:
                raise RuntimeError(f"[TidalMirror] Track {track_id} not found on Tidal")
            if r.status_code == 200:
                codec = r.headers.get("X-Codec", "flac").lower()
                ext = ".flac" if "flac" in codec else ".m4a"
                final_path = output_path + ext
                os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
                with open(final_path, "wb") as f:
                    for chunk in r.iter_content(131072):
                        if chunk:
                            f.write(chunk)
                # Tidal wraps FLAC in an M4A container (ISO Base Media / ftyp)
                # even when the codec header says "flac". Detect by magic bytes
                # and remux to a real FLAC file regardless of the reported extension.
                if self._is_m4a_by_magic(final_path):
                    import tempfile, shutil
                    tmp = final_path + ".tmp.m4a"
                    shutil.move(final_path, tmp)
                    if self._remux_m4a_to_flac(tmp, output_path + ".flac"):
                        os.remove(tmp)
                        return output_path + ".flac"
                    else:
                        # Remux failed — restore original and return as-is
                        shutil.move(tmp, final_path)
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
                timeout=30,
            )
            if r.status_code == 429:
                raise RateLimitedError("Tidal mirror rate limited (429)")
            if r.status_code == 503:
                self._reset_availability()
                raise RuntimeError("[TidalMirror] Server unavailable (503)")
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
            return self._download_segments(urls, output_path, ext)
        return self._download_single(stream_url, output_path, ext)

    def _download_single(self, url: str, output_base: str, ext: str) -> str:
        final_path = output_base + ext
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
        # Use a longer connect timeout but no read timeout — Tidal CDN URLs
        # expire in ~5 min but the download itself can take time on slow connections.
        with self._session.get(url, stream=True, timeout=(15, None)) as r:
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
                with self._session.get(url, stream=True, timeout=(15, None)) as r:
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
