import logging
import os
import time
import requests
from typing import Dict, List, Optional, Tuple

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.utils.matching import duration_close, score_similarity

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = 60
_CONSECUTIVE_FAILURE_THRESHOLD = 3
MIN_SIMILARITY = 0.25
REQUEST_TIMEOUT = 15  # seconds — proxy endpoints can be slow

class DabAdapter(BaseSourceAdapter):
    """
    Multi-endpoint Qobuz proxy adapter covering the community DAB/qbz mirror pool.

    Search is attempted against _SEARCH_ENDPOINTS in order (each has a /search and
    /stream API). On download, the same endpoint is tried first; if it fails or
    is in cooldown, the other search endpoints are tried, then _STREAM_ONLY_ENDPOINTS
    as a last resort (stream only, no search; compatible Qobuz track IDs).
    """

    name = "dab"
    priority = 2
    always_lossy = False

    def __init__(
        self,
        search_endpoints: Optional[List[str]] = None,
        stream_endpoints: Optional[List[str]] = None,
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        # Per-endpoint backoff: url -> unix timestamp when safe to retry
        self._dead_until: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        # Endpoint that succeeded on the last search (preferred for download)
        self._active_search_ep: Optional[str] = None
        self._search_endpoints: List[str] = [u.rstrip("/") for u in (search_endpoints or []) if u]
        self._stream_only_endpoints: List[str] = [u.rstrip("/") for u in (stream_endpoints or []) if u]

    # ------------------------------------------------------------------
    # Endpoint health helpers
    # ------------------------------------------------------------------

    def _is_dead(self, url: str) -> bool:
        return time.time() < self._dead_until.get(url, 0.0)

    def _mark_dead(self, url: str) -> None:
        self._dead_until[url] = time.time() + _BACKOFF_SECONDS
        self._failures[url] = self._failures.get(url, 0) + 1
        logger.debug(f"[Dab] {url} marked dead for {_BACKOFF_SECONDS}s")

    def _record_success(self, url: str) -> None:
        self._failures.pop(url, None)
        self._dead_until.pop(url, None)

    def _live_search_endpoints(self) -> List[str]:
        return [u for u in self._search_endpoints if not self._is_dead(u)]

    def _live_stream_endpoints(self) -> List[Tuple[str, str]]:
        """Return (base_url, style) pairs for all live stream endpoints."""
        result: List[Tuple[str, str]] = []
        # Preferred: the endpoint that worked in search
        if self._active_search_ep and not self._is_dead(self._active_search_ep):
            result.append((self._active_search_ep, "dab"))
        # Other search endpoints as stream fallback
        for u in self._search_endpoints:
            if u != self._active_search_ep and not self._is_dead(u):
                result.append((u, "dab"))
        # Stream-only endpoints last
        for u in self._stream_only_endpoints:
            if not self._is_dead(u):
                result.append((u, "qbz"))
        return result

    # ------------------------------------------------------------------
    # BaseSourceAdapter interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._search_endpoints)

    def is_throttled(self) -> bool:
        return len(self._live_search_endpoints()) == 0

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        if self.is_throttled():
            logger.debug("[Dab] All search endpoints in backoff — skipping")
            return None

        query = f"{track.primary_artist} {track.title}"

        for base_url in self._live_search_endpoints():
            logger.debug(f"[Dab] Searching via {base_url}: '{query}'")
            try:
                r = self._session.get(
                    f"{base_url}/search",
                    params={"q": query},
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code == 429:
                    self._mark_dead(base_url)
                    raise RateLimitedError(f"[Dab] {base_url} rate limited")
                r.raise_for_status()

                tracks = r.json().get("tracks", [])
                best_score = 0.0
                best_match: Optional[SearchResult] = None

                for item in tracks:
                    item_title = item.get("title", "")
                    item_artist = item.get("artist", "")
                    item_id = str(item.get("id", ""))
                    duration = item.get("duration")

                    score = score_similarity(
                        query_title=track.title,
                        query_artists=track.artists,
                        result_title=item_title,
                        result_artist=item_artist,
                    )
                    if duration and track.duration_seconds:
                        if not duration_close(track.duration_seconds, duration, tolerance=15):
                            score *= 0.8

                    if score > best_score:
                        best_score = score
                        best_match = SearchResult(
                            source=self.name,
                            title=item_title,
                            artists=[item_artist],
                            album=item.get("albumTitle"),
                            duration_ms=int(duration * 1000) if duration else None,
                            audio_format=AudioFormat.FLAC,
                            quality_kbps=None,
                            is_lossless=True,
                            bit_depth=16,
                            download_url=None,
                            stream_id=item_id,
                            similarity_score=score,
                        )

                if best_match and best_score >= MIN_SIMILARITY:
                    logger.debug(
                        f"[Dab] Match via {base_url} — score={best_score:.2f}: {best_match.title}"
                    )
                    self._active_search_ep = base_url
                    self._record_success(base_url)
                    return best_match

                # No sufficient match on this endpoint — try the next one
                logger.debug(f"[Dab] No match on {base_url} (best={best_score:.2f})")

            except RateLimitedError:
                continue
            except requests.Timeout:
                logger.debug(f"[Dab] {base_url} timed out on search")
                self._mark_dead(base_url)
                continue
            except Exception as e:
                logger.debug(f"[Dab] {base_url} search error: {e}")
                self._mark_dead(base_url)
                continue

        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        track_id = result.stream_id
        if not track_id:
            raise ValueError("[Dab] Missing track ID in search result")

        stream_endpoints = self._live_stream_endpoints()
        if not stream_endpoints:
            raise RateLimitedError("[Dab] All stream endpoints in backoff")

        last_exc: Exception = RuntimeError("[Dab] No stream endpoints available")

        for base_url, style in stream_endpoints:
            try:
                stream_url = self._resolve_stream_url(base_url, style, track_id)
                if not stream_url:
                    self._mark_dead(base_url)
                    continue

                final_path = output_path + ".flac"
                os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)
                logger.debug(f"[Dab] Downloading via {base_url} → {final_path}")

                with self._session.get(stream_url, stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    with open(final_path, "wb") as f:
                        for chunk in resp.iter_content(65536):
                            f.write(chunk)

                self._record_success(base_url)
                return final_path

            except RateLimitedError as e:
                last_exc = e
                self._mark_dead(base_url)
                continue
            except requests.Timeout:
                last_exc = RuntimeError(f"[Dab] {base_url} stream timed out")
                self._mark_dead(base_url)
                continue
            except Exception as e:
                last_exc = e
                self._mark_dead(base_url)
                continue

        raise RuntimeError(f"[Dab] All stream endpoints failed: {last_exc}")

    def _resolve_stream_url(self, base_url: str, style: str, track_id: str) -> Optional[str]:
        """Call the stream API and return the direct download URL, or None on failure."""
        if style == "dab":
            r = self._session.get(
                f"{base_url}/stream",
                params={"trackId": track_id},
                timeout=REQUEST_TIMEOUT,
            )
        else:  # qbz — REST-style path
            r = self._session.get(
                f"{base_url}/track/{track_id}",
                timeout=REQUEST_TIMEOUT,
            )

        if r.status_code == 429:
            raise RateLimitedError(f"[Dab] {base_url} rate limited on stream")
        r.raise_for_status()
        return r.json().get("url")

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        return not isinstance(error, RateLimitedError)


def _diagnose() -> None:
    """Run via: python -m antra.sources.dab"""
    logging.basicConfig(level=logging.DEBUG)
    adapter = DabAdapter(search_endpoints=[], stream_endpoints=[])

    track = TrackMetadata(
        title="Bad Guy",
        artists=["Billie Eilish"],
        album="WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?",
    )
    print("Searching for 'Bad Guy' by Billie Eilish across all endpoints...")
    res = adapter.search(track)
    if res:
        print(f"Found: {res.title} by {res.artists} — ID: {res.stream_id} (score: {res.similarity_score:.2f})")
        print(f"Active endpoint: {adapter._active_search_ep}")
    else:
        print("Not found — all search endpoints may be down or returning no match.")
        print(f"Dead endpoints: {[u for u in adapter._search_endpoints if adapter._is_dead(u)]}")


if __name__ == "__main__":
    _diagnose()
