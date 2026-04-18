import logging
import os
import time
import requests
from typing import Optional

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter, RateLimitedError
from antra.utils.matching import duration_close, score_similarity

logger = logging.getLogger(__name__)

# Mirrors the HiFi adapter's rate-limit resilience strategy:
# - Per-request 429 backoff with 60-second cooldown
# - Consecutive failure tracking → is_throttled() after 3 failures
# - Automatic reset on any successful search or download
_BACKOFF_SECONDS = 60
_CONSECUTIVE_FAILURE_THRESHOLD = 3
MIN_SIMILARITY = 0.25
REQUEST_TIMEOUT = 15  # seconds — dabmusic can be slow


class DabAdapter(BaseSourceAdapter):
    """
    DabMusic (dabmusic.xyz) adapter for free FLAC streams.
    Under the hood, these directly resolve to Qobuz high-quality FLAC streams.
    """

    name = "dab"
    priority = 2  # Amazon is 1, HiFi and DAB are both 2 (tied free-lossless tier)
    always_lossy = False

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        self._base_url = "https://dabmusic.xyz/api"
        # Rate-limit / throttle state (ported from HiFi adapter)
        self._backoff_until: float = 0.0  # unix timestamp when safe to retry
        self._consecutive_failures: int = 0

    def is_available(self) -> bool:
        """Available via HTTP requests; no dependencies required."""
        return True

    def is_throttled(self) -> bool:
        """
        Return True when Dab is rate-limited and should be skipped.

        Triggers when EITHER:
        - The endpoint is currently in its 429 cooldown window, OR
        - 3+ consecutive search/download failures have occurred this session
        """
        if time.time() < self._backoff_until:
            logger.debug(
                f"[Dab] Throttled: in 429 backoff until "
                f"{self._backoff_until - time.time():.0f}s from now"
            )
            return True
        if self._consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
            logger.debug(
                f"[Dab] Throttled: {self._consecutive_failures} consecutive failures"
            )
            return True
        return False

    def _mark_429(self) -> None:
        """Put the endpoint in a 60-second cooldown after receiving a 429."""
        self._backoff_until = time.time() + _BACKOFF_SECONDS
        logger.debug(f"[Dab] Backed off for {_BACKOFF_SECONDS}s after 429")

    def _record_success(self) -> None:
        """Reset failure counter on any successful operation."""
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Increment failure counter."""
        self._consecutive_failures += 1
        logger.debug(
            f"[Dab] Consecutive failures: {self._consecutive_failures}"
        )

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        # Skip entirely if we're in backoff / throttle
        if self.is_throttled():
            logger.debug("[Dab] Skipping search — adapter is throttled")
            return None

        query = f"{track.primary_artist} {track.title}"
        search_url = f"{self._base_url}/search"
        logger.debug(f"[Dab] Searching: '{query}'")

        try:
            r = self._session.get(search_url, params={"q": query}, timeout=REQUEST_TIMEOUT)

            if r.status_code == 429:
                self._mark_429()
                self._record_failure()
                raise RateLimitedError("[Dab] Rate limited on search endpoint")

            r.raise_for_status()
            data = r.json()
            
            # The format: {"tracks": [{"id": ..., "title": ...}]}
            tracks = data.get("tracks", [])
            
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
                        bit_depth=24,  # High-res FLAC via Qobuz backend
                        download_url=None,
                        stream_id=item_id,
                        similarity_score=score,
                    )
            
            if best_match and best_score >= MIN_SIMILARITY:
                logger.debug(f"[Dab] Best match score={best_score:.2f}: {best_match.title}")
                self._record_success()
                return best_match
            
            self._record_failure()
            logger.debug(f"[Dab] No sufficient match found (best score: {best_score:.2f})")
            return None

        except RateLimitedError:
            raise  # Re-raise so the engine skips to next source
        except Exception as e:
            self._record_failure()
            logger.debug(f"[Dab] Search failed: {e}")
            return None

    def download(self, result: SearchResult, output_path: str) -> str:
        # Skip if we're in backoff / throttle
        if self.is_throttled():
            raise RateLimitedError("[Dab] Skipping download — adapter is throttled")

        track_id = result.stream_id
        if not track_id:
            raise ValueError("[Dab] Missing track ID in search result")

        stream_api_url = f"{self._base_url}/stream"
        
        try:
            logger.debug(f"[Dab] Requesting stream URL for track_id: {track_id}")
            r = self._session.get(stream_api_url, params={"trackId": track_id}, timeout=REQUEST_TIMEOUT)

            if r.status_code == 429:
                self._mark_429()
                self._record_failure()
                raise RateLimitedError("[Dab] Rate limited on stream endpoint")

            r.raise_for_status()
            data = r.json()
            stream_url = data.get("url")

            if not stream_url:
                self._record_failure()
                raise RuntimeError("No stream URL in response")

            # Stream URL is an Akamai-hosted FLAC file (Qobuz CDN)
            final_path = output_path + ".flac"
            os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

            logger.debug(f"[Dab] Downloading stream to {final_path}")
            with self._session.get(stream_url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(final_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)

            self._record_success()
            return final_path
            
        except RateLimitedError:
            raise  # Re-raise for engine fallthrough
        except requests.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                self._mark_429()
                self._record_failure()
                raise RateLimitedError("[Dab] Rate limited on stream endpoint")
            self._record_failure()
            raise RuntimeError(f"HTTP error during download: {e}")
        except Exception as e:
            self._record_failure()
            raise RuntimeError(f"Failed to download stream: {e}")

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        """Don't retry after rate limits — let the engine fall through to next source."""
        if isinstance(error, RateLimitedError):
            return False
        return True

def _diagnose():
    """Run via: python -m antra.sources.dab"""
    logging.basicConfig(level=logging.DEBUG)
    adapter = DabAdapter()
    
    if not adapter.is_available():
        print("DabAdapter reports not available.")
        return

    track = TrackMetadata(
        title="Bad Guy",
        artists=["Billie Eilish"],
        album="WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?"
    )
    print("Searching for Bad Guy by Billie Eilish...")
    res = adapter.search(track)
    if res:
        print(f"Found: {res.title} by {res.artists} -> ID: {res.stream_id} (Score: {res.similarity_score:.2f})")
    else:
        print("Track not found.")

if __name__ == "__main__":
    _diagnose()
