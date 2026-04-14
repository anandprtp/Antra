"""
Abstract base class for all audio source adapters.
"""
from abc import ABC, abstractmethod
from typing import Optional

from antra.core.models import TrackMetadata, SearchResult, AudioFormat


class RateLimitedError(Exception):
    """
    Raised by a source adapter when it receives a 429 / rate-limit response.
    The engine treats this as a signal to skip this adapter immediately —
    no retry delay, no further attempts — and fall through to the next source.
    """


class BaseSourceAdapter(ABC):
    """
    All source adapters implement this interface.
    search() returns the best available SearchResult or None.
    download() saves the audio file to disk and returns the path.
    """

    name: str = "base"
    priority: int = 99  # Lower = higher priority
    always_lossy: bool = False  # True for adapters that can never return lossless audio

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if credentials/dependencies are configured."""
        ...

    @abstractmethod
    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        """
        Search for the track. Return best SearchResult or None.
        Implementations should:
          1. Try ISRC first if available
          2. Fall back to title + artist search
          3. Score similarity using utils.matching
          4. Return None if best score < threshold
        """
        ...

    @abstractmethod
    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Download audio to output_path (without extension).
        Return the full path with extension after successful download.
        Raise an exception on failure.
        """
        ...

    def hydrate_track_metadata(self, track: TrackMetadata, result: SearchResult) -> None:
        """Optionally enrich TrackMetadata using source-specific metadata."""
        return None

    def mark_failed_result(self, result: SearchResult, error: Exception) -> None:
        """Optionally blacklist a failed result so future searches can skip it."""
        return None

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        """Return False when retrying the same result would be wasted work."""
        return True

    def should_exclude_adapter_after_failure(
        self,
        result: SearchResult,
        error: Exception,
    ) -> bool:
        """Return False when the adapter can still provide alternative matches."""
        return True
