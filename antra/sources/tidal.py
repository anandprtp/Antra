"""
Tidal source adapter — lossless FLAC via tidalapi.

Install: pip install tidalapi

Tidal offers MQA (Master Quality Authenticated) and lossless FLAC.
This adapter uses the unofficial tidalapi library.

Mirror pool support: if premium/community mirror URLs are supplied via
the `mirrors` parameter, download() tries them first (returning the
stream URL directly) before falling back to tidalapi. This allows
24-bit HiRes FLAC from the premium server without MQA processing.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

import requests as _requests

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.sources.odesli import OdesliEnricher
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.80

_SUBPROCESS_FLAGS = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW


class TidalAdapter(BaseSourceAdapter):
    name = "tidal"
    priority = 1  # Prefer direct premium TIDAL ahead of lossy/community fallbacks

    def __init__(
        self,
        email: str = "",
        password: str = "",
        mirrors: Optional[list[str]] = None,
        enabled: bool = False,
        auth_mode: str = "session_json",
        session_json: str = "",
        access_token: str = "",
        refresh_token: str = "",
        session_id: str = "",
        token_type: str = "Bearer",
    ):
        self.email = email
        self.password = password
        self.enabled = enabled
        self.auth_mode = (auth_mode or "session_json").strip().lower()
        self.session_json = session_json
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.session_id = session_id
        self.token_type = token_type or "Bearer"
        self._session = None
        self._session_file_path: Optional[str] = None

        # Mirror pool for download (tried before tidalapi fallback).
        self._mirrors: list[str] = [m.rstrip("/") for m in (mirrors or []) if m]
        # Failure counter per mirror; >= 3 = excluded, 99 = permanently excluded.
        self._mirror_failures: dict[str, int] = {}
        self._odesli = OdesliEnricher()

    def is_available(self) -> bool:
        if self._mirrors:
            return True  # mirror-only mode works without credentials or tidalapi
        try:
            import tidalapi  # noqa
            return bool(self._has_direct_credentials())
        except ImportError:
            return False

    def _has_direct_credentials(self) -> bool:
        if self.enabled:
            if self.auth_mode == "session_json":
                return bool((self.session_json or "").strip())
            return bool((self.access_token or "").strip() and (self.refresh_token or "").strip())
        return bool(self.email and self.password)

    @staticmethod
    def _wrap_session_payload(payload: dict) -> dict:
        normalized = {}
        for key, value in payload.items():
            if isinstance(value, dict) and "data" in value:
                normalized[key] = value
            else:
                normalized[key] = {"data": value}
        if "is_pkce" not in normalized:
            normalized["is_pkce"] = {"data": True}
        return normalized

    def _build_session_payload(self) -> Optional[dict]:
        if not self.enabled:
            return None
        if self.auth_mode == "session_json":
            raw = (self.session_json or "").strip()
            if not raw:
                raise RuntimeError("TIDAL session JSON is empty")
            try:
                parsed = json.loads(raw)
            except Exception as e:
                raise RuntimeError(f"TIDAL session JSON is invalid: {e}") from e
            if not isinstance(parsed, dict):
                raise RuntimeError("TIDAL session JSON must be an object")
            return self._wrap_session_payload(parsed)

        access_token = (self.access_token or "").strip()
        refresh_token = (self.refresh_token or "").strip()
        if not access_token or not refresh_token:
            raise RuntimeError("TIDAL manual token mode requires access and refresh tokens")
        return {
            "token_type": {"data": (self.token_type or "Bearer").strip() or "Bearer"},
            "session_id": {"data": (self.session_id or "").strip()},
            "access_token": {"data": access_token},
            "refresh_token": {"data": refresh_token},
            "is_pkce": {"data": True},
        }

    def _ensure_session_file(self) -> Optional[str]:
        payload = self._build_session_payload()
        if payload is None:
            return None
        if self._session_file_path and os.path.exists(self._session_file_path):
            return self._session_file_path
        fd, path = tempfile.mkstemp(prefix="antra_tidal_", suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        self._session_file_path = path
        return path

    def _get_session(self):
        if self._session:
            return self._session
        try:
            import tidalapi
            import pathlib
            config = tidalapi.Config(quality=tidalapi.Quality.hi_res_lossless)
            session = tidalapi.Session(config)
            session_file = self._ensure_session_file()
            if session_file:
                ok = session.login_session_file(pathlib.Path(session_file), do_pkce=True, fn_print=lambda _msg: None)
                if not ok:
                    raise RuntimeError("session login returned false")
            else:
                session.login(self.email, self.password)
            self._session = session
            logger.info("[Tidal] Logged in successfully.")
            return session
        except Exception as e:
            raise RuntimeError(f"Tidal login failed: {e}")

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            session = self._get_session()
        except Exception as e:
            logger.warning(f"[Tidal] {e}")
            return None

        direct = self._search_by_platform_id(track, session)
        if direct:
            return direct

        query = f"{track.title} {track.primary_artist}"
        try:
            import tidalapi
            logger.debug(
                "[Tidal] Searching catalog query=%r title=%r artist=%r isrc=%r duration=%s",
                query,
                track.title,
                track.primary_artist,
                track.isrc,
                track.duration_seconds,
            )
            results = session.search(query, models=[tidalapi.Track], limit=10)
            tidal_tracks = results.get("tracks", [])
        except Exception as e:
            logger.warning(f"[Tidal] Search failed: {e}")
            return None

        best = None
        best_score = 0.0
        candidate_count = 0

        for t in tidal_tracks:
            candidate_count += 1
            artist_name = t.artist.name if hasattr(t, "artist") else ""
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=t.name,
                result_artist=artist_name,
            )

            # ISRC check
            isrc_match = False
            if track.isrc and hasattr(t, "isrc") and t.isrc:
                if t.isrc.upper() == track.isrc.upper():
                    score = 1.0
                    isrc_match = True

            if hasattr(t, "duration") and track.duration_seconds:
                if not duration_close(track.duration_seconds, t.duration, tolerance=5):
                    score *= 0.8

            # Quality boost: prefer 24-bit HiRes versions over 16-bit FLAC versions of the same track
            audio_quality = getattr(t, "audio_quality", "")
            is_hires = audio_quality in ("HI_RES", "HI_RES_LOSSLESS") or getattr(t, "is_hi_res_lossless", False)
            bit_depth = 24 if is_hires else 16
            sample_rate = 96000 if is_hires else 44100
            if is_hires:
                score += 0.05

            if score > best_score:
                best_score = score
                best = self._track_to_result(t, score=score, isrc_match=isrc_match)

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(
                "[Tidal] Search accepted score=%.2f id=%s title=%r album=%r",
                best_score,
                best.stream_id,
                best.title,
                best.album,
            )
            return best

        if best:
            logger.info(
                "[Tidal] No acceptable search match for %r by %s "
                "(query=%r candidates=%d best_score=%.2f best_id=%s best_title=%r best_album=%r isrc=%r)",
                track.title,
                track.artist_string,
                query,
                candidate_count,
                best_score,
                best.stream_id,
                best.title,
                best.album,
                track.isrc,
            )
        else:
            logger.info(
                "[Tidal] Search returned no candidates for %r by %s (query=%r isrc=%r)",
                track.title,
                track.artist_string,
                query,
                track.isrc,
            )
        return None

    def _search_by_platform_id(self, track: TrackMetadata, session) -> Optional[SearchResult]:
        """Resolve a cross-platform TIDAL ID first, bypassing fuzzy catalog search."""
        if not (track.isrc or track.spotify_id):
            return None
        try:
            platform_ids = self._odesli.resolve(track)
        except Exception as e:
            logger.debug(f"[Tidal] Platform ID resolution failed for '{track.title}': {e}")
            return None

        tidal_id = platform_ids.get("tidal")
        if not tidal_id:
            logger.debug(
                "[Tidal] No TIDAL platform ID for %r (resolved=%s isrc=%r spotify_id=%r)",
                track.title,
                sorted(platform_ids.keys()),
                track.isrc,
                track.spotify_id,
            )
            return None

        try:
            tidal_track = session.track(int(tidal_id))
            result = self._track_to_result(
                tidal_track,
                score=1.0,
                isrc_match=bool(track.isrc and getattr(tidal_track, "isrc", "").upper() == track.isrc.upper()),
            )
            logger.info(
                "[Tidal] Resolved platform ID for %r: tidal_id=%s title=%r album=%r",
                track.title,
                tidal_id,
                result.title,
                result.album,
            )
            return result
        except Exception as e:
            logger.warning(f"[Tidal] TIDAL ID {tidal_id} resolved but could not be loaded: {e}")
            return None

    def _track_to_result(
        self,
        tidal_track,
        *,
        score: float,
        isrc_match: bool = False,
    ) -> SearchResult:
        artist_name = tidal_track.artist.name if hasattr(tidal_track, "artist") else ""
        audio_quality = getattr(tidal_track, "audio_quality", "")
        is_hires = audio_quality in ("HI_RES", "HI_RES_LOSSLESS") or getattr(
            tidal_track, "is_hi_res_lossless", False
        )
        bit_depth = 24 if is_hires else 16
        sample_rate = 96000 if is_hires else 44100
        return SearchResult(
            source=self.name,
            title=tidal_track.name,
            artists=[artist_name] if artist_name else [],
            album=tidal_track.album.name if hasattr(tidal_track, "album") else None,
            duration_ms=int(tidal_track.duration * 1000) if hasattr(tidal_track, "duration") else None,
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=bit_depth,
            sample_rate_hz=sample_rate,
            download_url=None,
            stream_id=str(tidal_track.id),
            similarity_score=score,
            isrc_match=isrc_match,
            is_explicit=getattr(tidal_track, "explicit", None),
        )

    def _try_mirror_download(self, track_id: int, output_path: str) -> Optional[str]:
        """
        Attempt to download via the mirror pool.
        Returns the final file path on success, None if all mirrors fail.
        403/503 on premium URLs permanently removes them from the pool.
        """
        for mirror in list(self._mirrors):
            if self._mirror_failures.get(mirror, 0) >= 3:
                continue

            api_url = f"{mirror}/api/track/{track_id}"
            headers: dict = {}

            try:
                resp = _requests.get(api_url, timeout=20, headers=headers)

                if resp.status_code == 200:
                    data = resp.json()
                    stream_url = data.get("streamUrl")
                    if not stream_url:
                        self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1
                        continue

                    codec = (data.get("codec") or "").lower()
                    bit_depth = data.get("bitDepth") or 16
                    ext = ".flac" if "flac" in codec else ".m4a"
                    final_path = output_path + ext
                    os.makedirs(os.path.dirname(final_path), exist_ok=True)

                    with _requests.get(stream_url, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(final_path, "wb") as f:
                            for chunk in r.iter_content(65536):
                                f.write(chunk)

                    logger.info(
                        f"[Tidal] Downloaded via mirror (codec={codec or 'unknown'}, {bit_depth}-bit)"
                    )
                    return final_path

                # 403/503 from mirror = permanently excluded.
                if resp.status_code in (403, 503):
                    logger.debug(
                        f"[Tidal] Mirror returned {resp.status_code} — removing from pool for session"
                    )
                    self._mirror_failures[mirror] = 99
                    continue

                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

            except Exception as e:
                logger.debug(f"[Tidal] Mirror failed: {e}")
                self._mirror_failures[mirror] = self._mirror_failures.get(mirror, 0) + 1

        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Download Tidal track as FLAC.

        Try mirror pool first (premium server gives 24-bit HiRes FLAC without MQA).
        Fall back to tidalapi stream URL when all mirrors fail or are unavailable.
        """
        track_id_str = result.stream_id
        track_id = int(track_id_str) if track_id_str else None

        # Mirror path — try before tidalapi so premium quality is preferred.
        if track_id and self._mirrors:
            mirror_path = self._try_mirror_download(track_id, output_path)
            if mirror_path:
                return mirror_path

        # tidalapi fallback — only if credentials are configured
        if not self._has_direct_credentials():
            raise RuntimeError("[Tidal] All mirrors failed and no credentials configured for direct fallback")
        session = self._get_session()
        if track_id is None:
            raise ValueError("[Tidal] Missing stream_id in search result")

        try:
            track = session.track(track_id)
            stream_data = self._resolve_direct_stream(track, session)
            urls = stream_data["urls"]
            ext = stream_data["ext"]
            is_segmented = stream_data["segmented"]

            if is_segmented:
                return self._download_segments(urls, output_path, ext)

            stream_url = urls[0]
            return self._download_single_url(stream_url, output_path, ext)

        except Exception as e:
            raise RuntimeError(f"Tidal download failed for track {track_id}: {e}")

    def _resolve_direct_stream(self, track, session) -> dict:
        """Resolve a PKCE-safe direct audio URL from TIDAL playback metadata."""
        import tidalapi
        from tidalapi.media import StreamManifest

        original_quality = session.config.quality
        quality_attempts = [
            getattr(tidalapi.Quality, "hi_res_lossless", original_quality),
            getattr(tidalapi.Quality, "high_lossless", original_quality),
        ]

        attempted: list[str] = []
        last_error: Exception | None = None

        try:
            for quality in quality_attempts:
                if quality in attempted:
                    continue
                attempted.append(quality)
                session.config.quality = quality
                try:
                    stream = track.get_stream()
                    manifest = StreamManifest(stream)
                    urls = manifest.get_urls() or []
                    if not urls:
                        raise RuntimeError("playback manifest returned no URLs")

                    codec = (manifest.get_codecs() or "").upper()
                    ext = ".m4a" if manifest.get_file_extension(urls[0], manifest.get_codecs()) in {".m4a", ".mp4"} else ".flac"
                    segmented = len(urls) > 1
                    logger.info(
                        "[Tidal] Using playback manifest quality=%s codec=%s bit_depth=%s sample_rate=%s segments=%s",
                        getattr(stream, "audio_quality", quality),
                        codec or "unknown",
                        getattr(stream, "bit_depth", None),
                        getattr(stream, "sample_rate", None),
                        len(urls),
                    )
                    return {"urls": urls, "ext": ext, "segmented": segmented}
                except Exception as e:
                    last_error = e
                    logger.debug(
                        "[Tidal] Playback manifest unavailable for track %s at quality=%s: %s",
                        getattr(track, "id", "unknown"),
                        quality,
                        e,
                    )
                    continue
        finally:
            session.config.quality = original_quality

        if last_error:
            raise last_error
        raise RuntimeError("unable to resolve stream manifest")

    def _download_single_url(self, url: str, output_base: str, ext: str) -> str:
        import requests

        with requests.get(url, stream=True) as r:
            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "").lower()
            final_ext = ext or ".flac"
            if "mp4" in content_type or "m4a" in content_type or "alac" in content_type:
                final_ext = ".m4a"
            elif url.split("?")[0].endswith((".m4a", ".mp4")):
                final_ext = ".m4a"
            elif url.split("?")[0].endswith(".flac"):
                final_ext = ".flac"

            final_path = output_base + final_ext
            os.makedirs(os.path.dirname(final_path), exist_ok=True)

            with open(final_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)

        return final_path

    def _stream_url_to_file(self, url: str, path: str) -> None:
        with _requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)

    def _download_segments(self, urls: list[str], output_base: str, ext: str) -> str:
        tmp_dir = tempfile.mkdtemp(prefix="antra_tidal_segments_")
        segment_files: list[str] = []

        try:
            for i, url in enumerate(urls):
                seg_path = os.path.join(tmp_dir, f"seg_{i:05d}.part")
                self._stream_url_to_file(url, seg_path)
                segment_files.append(seg_path)

            if not segment_files:
                raise RuntimeError("no TIDAL segments downloaded")

            final_path = output_base + ext
            os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

            if self._try_ffmpeg_concat(segment_files, final_path):
                return final_path

            with open(final_path, "wb") as out:
                for seg in segment_files:
                    with open(seg, "rb") as f:
                        out.write(f.read())
            return final_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _try_ffmpeg_concat(self, segment_files: list[str], output_path: str) -> bool:
        try:
            from antra.utils.runtime import get_ffmpeg_exe, get_clean_subprocess_env

            ffmpeg = get_ffmpeg_exe() or "ffmpeg"
            tmp_list = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            try:
                for seg in segment_files:
                    tmp_list.write(f"file '{seg}'\n")
            finally:
                tmp_list.close()

            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", tmp_list.name,
                    "-c", "copy",
                    output_path,
                ],
                capture_output=True,
                timeout=180,
                env=get_clean_subprocess_env(),
                **_SUBPROCESS_FLAGS,
            )
            os.unlink(tmp_list.name)
            if result.returncode != 0:
                logger.debug(
                    "[Tidal] ffmpeg concat failed: %s",
                    (result.stderr or result.stdout).decode(errors="ignore") if isinstance(result.stderr, bytes) else (result.stderr or result.stdout),
                )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"[Tidal] ffmpeg concat error: {e}")
            return False
