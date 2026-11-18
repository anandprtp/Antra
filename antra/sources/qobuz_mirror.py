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
from antra.utils.matching import score_similarity, duration_close, strip_collab

from antra.utils.mirror_http import TlsFallbackSession, ensure_complete_transfer, humanize_network_error

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 45

MIN_SIMILARITY = 0.55


class QobuzMirrorAdapter(BaseSourceAdapter):
    """
    Downloads 24-bit FLAC from your self-hosted Qobuz mirror server.
    """

    name = "qobuz_mirror"
    priority = 1  # Highest — 24-bit FLAC

    def __init__(self, mirror_url: str, api_key: str = "", preferred_output_format: str = "source"):
        self._base = mirror_url.rstrip("/")
        # v1.1.8 BUG-2: TLS-resilient session (curl_cffi fallback + proxy
        # diagnostics). Lifted from the Amazon adapter so all mirrors
        # behave the same on hostile networks.
        self._session = TlsFallbackSession("QobuzMirror")
        self._session.headers.update({"User-Agent": "Antra/1.0", "Accept": "application/json"})
        if api_key:
            self._session.headers["X-API-Key"] = api_key
        self._available: Optional[bool] = None
        self._timeout_until = 0.0  # monotonic; set when the mirror hangs on a search
        self._preferred_output_format = (preferred_output_format or "source").lower()

    def _requires_strict_24bit(self) -> bool:
        return self._preferred_output_format in {"lossless-24", "alac-24"}

    def is_available(self) -> bool:
        if not self._base:
            return False
        if self._available is not None:
            return self._available
        try:
            r = self._session.get(f"{self._base}/", timeout=15)
            self._available = r.status_code == 200
        except Exception:
            # Retry once after a short delay — handles brief restart windows where
            # the server is healthy but momentarily mid-restart.
            import time
            time.sleep(3)
            try:
                r = self._session.get(f"{self._base}/", timeout=15)
                self._available = r.status_code == 200
            except Exception:
                self._available = False
        return self._available

    def _reset_availability(self):
        self._available = None

    @staticmethod
    def _text_search_queries(track: TrackMetadata) -> list[dict[str, str]]:
        clean_title = strip_collab(track.title)
        artist_candidates: list[str] = []

        def add_artist(value: str) -> None:
            value = (value or "").strip()
            if value and value.lower() not in {a.lower() for a in artist_candidates}:
                artist_candidates.append(value)

        add_artist(track.primary_artist)
        add_artist(track.artist_string)
        for artist in track.artists:
            add_artist(artist)
        for artist in getattr(track, "album_artists", None) or []:
            add_artist(artist)

        title_candidates = [clean_title]
        if clean_title.lower() != track.title.lower():
            title_candidates.append(track.title)

        queries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_query(title: str, artist: str) -> None:
            title = (title or "").strip()
            artist = (artist or "").strip()
            if not title:
                return
            key = (title.lower(), artist.lower())
            if key in seen:
                return
            seen.add(key)
            queries.append({"title": title, "artist": artist})

        for title in title_candidates:
            for artist in artist_candidates:
                add_query(title, artist)
        # Some catalog search endpoints rank a distinctive title better without
        # a partial/reversed artist credit. The result is still validated below.
        for title in title_candidates:
            add_query(title, "")
        album = (track.album or "").strip()
        if album:
            for title in title_candidates:
                augmented_title = f"{title} {album}".strip()
                if augmented_title.lower() != title.lower():
                    for artist in artist_candidates:
                        add_query(augmented_title, artist)
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

    def _direct_result(self, track: TrackMetadata, track_id: str) -> SearchResult:
        """The exact Qobuz track a Qobuz link named - no searching at all.

        bit depth / sample rate are not in the album payload and
        /api/meta/track/{id} returns them null, so they fall back to the
        same 24/44100 the ISRC path already assumes when the server omits
        them. They are advisory: strict_24 is enforced at download time.
        """
        logger.info(
            "[QobuzMirror] Using known Qobuz track ID %s for '%s'",
            track_id, track.title,
        )
        return SearchResult(
            source=self.name,
            title=track.title,
            artists=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            bit_depth=24,
            sample_rate_hz=44100,
            download_url=None,
            stream_id=str(track_id),
            similarity_score=1.0,
            isrc_match=bool(track.isrc),
            is_explicit=track.is_explicit,
        )

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        # A Qobuz link already told us which track this is. Searching for
        # it by ISRC or title is guesswork over a fact we were handed, and
        # it loses: the mirror's ISRC endpoint walks Qobuz's text index,
        # which does not carry brand-new small-label releases.
        qobuz_track_id = getattr(track, "qobuz_track_id", None)
        if qobuz_track_id:
            return self._direct_result(track, str(qobuz_track_id))

        # A hung mirror answers every search with a 10s read timeout; ISRC + 3 text
        # queries then cost 40s per track. Back off for 30s instead of repeating it.
        import time as _t
        if _t.monotonic() < self._timeout_until:
            return None

        if track.isrc:
            try:
                r = self._session.get(
                    f"{self._base}/api/search/isrc/{track.isrc}",
                    timeout=_SEARCH_TIMEOUT,
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
                    if self._has_severe_duration_mismatch(track.duration_ms, result_duration_ms):
                        logger.info(
                            "[QobuzMirror] ISRC match for '%s' rejected — severe duration mismatch "
                            "(expected %.0fs, got %.0fs)",
                            track.title,
                            (track.duration_ms or 0) / 1000,
                            (result_duration_ms or 0) / 1000,
                        )
                        return self._text_search(track)
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
                        source_metadata=_extract_qobuz_mirror_source_meta(data),
                    )
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[QobuzMirror] ISRC search failed: %s", e)
                if "timed out" in str(e).lower():
                    self._timeout_until = _t.monotonic() + 30
                    return None

        # Text search fallback — used when ISRC is unavailable
        return self._text_search(track)

    def _text_search(self, track: TrackMetadata) -> Optional[SearchResult]:
        best: Optional[SearchResult] = None
        best_score = 0.0
        for query in self._text_search_queries(track):
            try:
                r = self._session.get(
                    f"{self._base}/api/search",
                    params={"title": query["title"], "artist": query["artist"], "limit": 5},
                    timeout=_SEARCH_TIMEOUT,
                )
                if r.status_code == 429:
                    raise RateLimitedError("Qobuz mirror rate limited (429)")
                if r.status_code in (401, 403):
                    logger.warning("[QobuzMirror] API key rejected (%d) — check key on server", r.status_code)
                    return None
                if r.status_code != 200:
                    continue
                items = r.json().get("results") or []
            except RateLimitedError:
                raise
            except Exception as e:
                logger.debug("[QobuzMirror] Text search failed: %s", e)
                if "timed out" in str(e).lower():
                    import time as _t
                    self._timeout_until = _t.monotonic() + 30
                    break
                continue

            for item in items:
                score = score_similarity(
                    query_title=track.title,
                    query_artists=track.artists,
                    result_title=item.get("title", ""),
                    result_artist=item.get("artist", ""),
                )
                dur_ms = item.get("duration_ms")
                if dur_ms and track.duration_ms:
                    if self._has_severe_duration_mismatch(track.duration_ms, dur_ms):
                        continue
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
                        source_metadata=_extract_qobuz_mirror_source_meta(item),
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
                params={"strict_24": "1"} if self._requires_strict_24bit() else None,
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
            if r.status_code == 404:
                # The mirror answers 404 only after walking its ENTIRE account
                # pool, and puts the reason in the JSON body. raise_for_status()
                # throws that body away, leaving the user a bare
                # "404 Client Error: Not Found for url: ..." -- and leaving
                # should_retry_download's rights-phrase tests matching nothing.
                # Keep the literal "404 Client Error" so the classifier still
                # fires even when the body is unparseable.
                detail = _mirror_detail(r)
                raise RuntimeError(
                    "[QobuzMirror] Stream request failed: 404 Client Error"
                    + (" - " + detail if detail else "")
                )
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
        # Atomic write (v1.1.8 BUG-4): stream into a .part file and only move it
        # into place once the transfer completes. Previously this streamed
        # straight into final_path, so a connection dropped mid-transfer left a
        # partial, untagged file sitting in the library — the reported
        # "1.2 MB won't play, no metadata" case. A partial can now never appear
        # under the real filename.
        # NOTE: the .part name is derived from output_path (the stem), NOT from
        # final_path. `final_path + ".part"` would be 5 characters LONGER than the
        # file we are about to write, which on Windows (MAX_PATH 260, and
        # LongPathsEnabled is 0 on a default install) can push a path that would
        # otherwise have worked over the limit — turning a working download into
        # a FileNotFoundError. Deep classical box sets hit this easily. Using the
        # stem keeps the temp name exactly as long as the final one.
        part_path = output_path + ".part"
        try:
            written = 0
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(131072):
                    if chunk:
                        written += len(chunk)
                        f.write(chunk)
            ensure_complete_transfer(r, written, "QobuzMirror")
            os.replace(part_path, final_path)
        except BaseException:
            try:
                os.remove(part_path)
            except OSError:
                pass
            raise
        finally:
            r.close()   # the response was opened without `with`; error paths leaked the connection

        logger.info("[QobuzMirror] Downloaded %s quality=%s bit_depth=%s",
                    os.path.basename(final_path), quality_hdr, bit_depth_hdr)
        return final_path

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        msg = str(error).lower()
        if "duration mismatch" in msg or "preview clip" in msg:
            return False
        # DEFINITIVE answer from the mirror: it walked its entire account pool
        # and nobody is licensed for this release. Retrying re-walks all 16
        # accounts for the same answer -- measured 12-22s each, three times --
        # and as a 500 it also tripped the engine's 5xx circuit breaker, which
        # then blocked Qobuz for 5 minutes for tracks it COULD have served.
        # A 404 from the mirror is likewise definitive, never worth a retry.
        if ("no account in the pool holds streaming rights" in msg
                or "not streamable by any of" in msg
                or "404 client error" in msg):
            return False
        # Cloudflare/VPS 5xx during the relay stream is usually not fixed by
        # hammering the same signed URL 2 more times. Fall through immediately
        # so the engine can try Tidal/Amazon/Apple instead.
        if any(code in msg for code in (" 524 ", "524 server error", "502", "503", "504")):
            return False
        return True


def _mirror_detail(resp) -> str:
    """Best-effort read of the mirror's JSON `detail` from an error response.

    Never raises and never blocks: the body of a non-2xx from this mirror is a
    small JSON object, but the response was opened with stream=True, so this is
    deliberately defensive. Returns "" when there is nothing usable.
    """
    try:
        resp.encoding = resp.encoding or "utf-8"
        data = resp.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if not isinstance(detail, str):
        return ""
    return " ".join(detail.split())[:300]


def _extract_qobuz_mirror_source_meta(data: dict) -> dict:
    meta: dict = {}
    if isinstance(data.get("explicit"), bool):
        meta["is_explicit"] = data["explicit"]
    if data.get("isrc"):
        meta["isrc"] = str(data["isrc"])
    if data.get("genre"):
        g = data["genre"]
        meta["genres"] = [str(g)] if isinstance(g, str) else list(g)
    if data.get("composer"):
        meta["composer"] = str(data["composer"])
    if data.get("label"):
        meta["label"] = str(data["label"])
    return meta
