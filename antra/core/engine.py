"""
Download engine — orchestrates resolve → download → tag → organize.
"""
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from mutagen import File as MutagenFile

from antra.core.control import DownloadController
from antra.core.events import EngineEvent, EngineEventType
from antra.core.models import AudioFormat, TrackMetadata, DownloadResult, DownloadStatus
from antra.core.resolver import SourceResolver
from antra.utils.lyrics import LyricsFetcher
from antra.utils.organizer import LibraryOrganizer
from antra.utils.tagger import FileTagger
from antra.utils.transcoder import AudioTranscoder

logger = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    max_retries: int = 3
    retry_delay: float = 5.0
    fetch_lyrics: bool = True
    fetch_artwork: bool = True
    output_format: str = "source"


class DownloadEngine:
    def __init__(
        self,
        resolver: SourceResolver,
        organizer: LibraryOrganizer,
        lyrics_fetcher: Optional[LyricsFetcher] = None,
        config: Optional[EngineConfig] = None,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        controller: Optional[DownloadController] = None,
    ):
        self.resolver = resolver
        self.organizer = organizer
        self.lyrics = lyrics_fetcher
        self.tagger = FileTagger()
        self.transcoder = AudioTranscoder()
        self.cfg = config or EngineConfig()
        self.event_callback = event_callback
        self.controller = controller

    def _emit(self, event_type: EngineEventType, **kwargs):
        if not self.event_callback:
            return
        try:
            self.event_callback(EngineEvent(type=event_type, **kwargs))
        except Exception as e:
            logger.debug(f"Event callback failed: {e}")

    @staticmethod
    def _hydrate_track_metadata(track: TrackMetadata, result) -> None:
        if (not track.album or track.album == "Unknown Album") and result.album:
            track.album = result.album
        if not track.artwork_url and getattr(result, "artwork_url", None):
            track.artwork_url = result.artwork_url

    def _fetch_lyrics_if_needed(self, track: TrackMetadata) -> None:
        if not self.cfg.fetch_lyrics or not self.lyrics:
            return
        if track.lyrics or track.synced_lyrics:
            return
        try:
            plain, synced = self.lyrics.fetch(track)
            track.lyrics = plain
            track.synced_lyrics = synced
        except Exception as e:
            logger.debug(f"  ℹ  Lyrics fetch failed: {e}")

    @staticmethod
    def _audio_format_from_path(file_path: str) -> AudioFormat | None:
        ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else ""
        return {
            "flac": AudioFormat.FLAC,
            "mp3": AudioFormat.MP3,
            "aac": AudioFormat.AAC,
            "m4a": AudioFormat.AAC,
        }.get(ext)

    def _should_convert_output(self, file_path: str, output_format: str) -> bool:
        return self.transcoder.needs_conversion(file_path, output_format)

    def _requires_lossless_output(self) -> bool:
        return self.cfg.output_format in {"flac", "lossless"}

    @staticmethod
    def _probe_duration_seconds(file_path: str) -> float | None:
        try:
            audio = MutagenFile(file_path)
        except Exception:
            return None
        if not audio or not getattr(audio, "info", None):
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)
        length = getattr(audio.info, "length", None)
        if length is None:
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)
        try:
            return float(length)
        except (TypeError, ValueError):
            return DownloadEngine._probe_duration_seconds_with_ffprobe(file_path)

    @staticmethod
    def _probe_duration_seconds_with_ffprobe(file_path: str) -> float | None:
        if not shutil.which("ffprobe"):
            return None
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            return float(result.stdout.strip())
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_truncated_download(cls, file_path: str, expected_duration_ms: int | None) -> bool:
        if not expected_duration_ms or expected_duration_ms < 60000:
            return False
        actual_seconds = cls._probe_duration_seconds(file_path)
        if actual_seconds is None:
            return False
        expected_seconds = expected_duration_ms / 1000.0
        return (
            actual_seconds < expected_seconds * 0.8
            and (expected_seconds - actual_seconds) >= 20
        )

    @staticmethod
    def _discard_file(path: str) -> None:
        import os

        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def download_track(
        self,
        track: TrackMetadata,
        track_index: Optional[int] = None,
        track_total: Optional[int] = None,
    ) -> DownloadResult:
        """Full pipeline for a single track."""

        # 1. Resume check
        existing = self.organizer.is_already_downloaded(track)
        if existing:
            existing = self.organizer.ensure_playlist_copy(track, existing)
            logger.info(f"  [SKIP]  Skipping (already downloaded): {track.title}")
            self._emit(
                EngineEventType.TRACK_SKIPPED,
                track=track,
                track_index=track_index,
                track_total=track_total,
                file_path=existing,
                message="Track already exists on disk.",
            )
            return DownloadResult(
                track=track,
                status=DownloadStatus.SKIPPED,
                file_path=existing,
            )

        # 2. Fetch lyrics once (before download, non-blocking)
        self._fetch_lyrics_if_needed(track)

        excluded_adapters: set[str] = set()
        last_error: Optional[str] = None
        last_source: Optional[str] = None

        while True:
            # 3. Resolve
            resolution = self.resolver.resolve(track, excluded_adapters=excluded_adapters)
            if not resolution:
                logger.warning(f"  [FAIL]  No source found: {track.title}")
                self.organizer.mark_failed(track, last_error or "no_source")
                self._emit(
                    EngineEventType.TRACK_FAILED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    source=last_source,
                    error=last_error or "No matching source found",
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.FAILED,
                    source_used=last_source,
                    error_message=last_error or "No matching source found",
                    attempt_count=self.cfg.max_retries,
                )

            result, adapter = resolution
            if self._requires_lossless_output() and not result.is_lossless:
                last_error = (
                    f"[{adapter.name}] Rejected lossy result in lossless mode for {track.title}"
                )
                last_source = adapter.name
                excluded_adapters.add(adapter.name)
                logger.warning(f"  [WARN]  {last_error}")
                logger.info(f"  [NEXT]  {adapter.name} cannot satisfy lossless output, trying next source...")
                continue
            self._hydrate_track_metadata(track, result)
            adapter.hydrate_track_metadata(track, result)
            self._fetch_lyrics_if_needed(track)
            # Layout must use post-hydration metadata (album/year from the resolver, etc.)
            output_base = self.organizer.get_output_path(track)
            self._emit(
                EngineEventType.TRACK_RESOLVED,
                track=track,
                track_index=track_index,
                track_total=track_total,
                source=adapter.name,
                quality_label=result.quality_label,
                message=f"Resolved via {adapter.name}",
            )

            file_path: Optional[str] = None
            final_error: Optional[Exception] = None

            for attempt in range(1, self.cfg.max_retries + 1):
                import time
                self._last_attempt_start = time.time()
                try:
                    source_text = adapter.name
                    if adapter.name == "soulseek" and result.stream_id:
                        parts = str(result.stream_id).split("|")
                        if len(parts) >= 1:
                            source_text = f"soulseek({parts[0]})"
                            
                    self._emit(
                        EngineEventType.TRACK_DOWNLOAD_ATTEMPT,
                        track=track,
                        track_index=track_index,
                        track_total=track_total,
                        source=source_text,
                        quality_label=result.quality_label,
                        attempt=attempt,
                    )
                    source_quality = result.quality_label
                    if getattr(result, "sample_rate", None):
                        source_quality += f" / {result.sample_rate / 1000}kHz"
                    
                    logger.info(
                        f"  \U0001f4e5 [Downloading] [{track_index}/{track_total}] {track.title} by {track.artist_string} ({source_quality})"
                    )
                    candidate_path = adapter.download(result, output_base)
                    if self._should_convert_output(candidate_path, self.cfg.output_format):
                        logger.info(f"  [FMT]  Converting to {self.cfg.output_format}: {track.title}")
                        candidate_path = self.transcoder.convert(candidate_path, self.cfg.output_format)
                    if self._is_truncated_download(candidate_path, track.duration_ms):
                        self._discard_file(candidate_path)
                        raise RuntimeError(
                            f"[{adapter.name}] Download appears truncated for {track.title}"
                        )
                    file_path = candidate_path
                    break
                except Exception as e:
                    final_error = e
                    last_error = str(e)
                    last_source = adapter.name
                    adapter.mark_failed_result(result, e)
                    will_retry = attempt < self.cfg.max_retries and adapter.should_retry_download(result, e)
                    if adapter.name == "hifi" and "all quality levels failed" in str(e).lower():
                        logger.info("  [INFO]  HiFi mirrors could not provide a valid stream. Trying next source...")
                    elif "rate limited" in str(e).lower() or "429" in str(e):
                        logger.debug(f"  [RATE]  Attempt {attempt} rate-limited, retrying...")
                    elif will_retry:
                        # Transient failure — more attempts coming, keep it quiet
                        logger.debug(f"  [RETRY] Attempt {attempt} failed, retrying... ({e})")
                    else:
                        # Final failure for this adapter — surface it
                        logger.warning(f"  [WARN]  Attempt {attempt} failed: {e}")
                    if will_retry:
                        time.sleep(self.cfg.retry_delay)
                        continue
                    break

            if file_path:
                # 4. Tag
                logger.debug(
                    "  [TAG]  %s | album=%r artwork=%s lyrics=%s synced=%s",
                    file_path,
                    track.album,
                    bool(track.artwork_url),
                    bool(track.lyrics),
                    bool(track.synced_lyrics),
                )
                tag_ok = self.tagger.tag(file_path, track)
                if not tag_ok:
                    logger.warning(
                        f"  [WARN]  Metadata tagging did not complete for {file_path}. "
                        "This usually means the output container is unsupported for embedded tags."
                    )

                # 5. Mark done
                self.organizer.mark_downloaded(track, file_path)

                import os, time
                size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
                attempt_time = getattr(self, "_last_attempt_start", time.time())
                elapsed = time.time() - attempt_time
                
                logger.info(
                    f"  \u2728 [Complete] [{track_index}/{track_total}] {track.title} by {track.artist_string}"
                )
                self._emit(
                    EngineEventType.TRACK_COMPLETED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    source=adapter.name,
                    file_path=file_path,
                    quality_label=result.quality_label,
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.COMPLETED,
                    file_path=file_path,
                    source_used=adapter.name,
                    audio_format=self._audio_format_from_path(file_path) or result.audio_format,
                )

            should_exclude = True
            if final_error is not None:
                should_exclude = adapter.should_exclude_adapter_after_failure(result, final_error)

            if should_exclude:
                excluded_adapters.add(adapter.name)
                logger.info(f"  [NEXT]  {adapter.name} failed after retries, trying next source...")
            else:
                logger.info(f"  [NEXT]  {adapter.name} candidate failed, trying another match from the same source...")

    def download_playlist(self, tracks: list[TrackMetadata]) -> list[DownloadResult]:
        """Download all tracks in a playlist, returning all results."""
        total = len(tracks)
        results: list[DownloadResult] = []
        playlist_name = tracks[0].playlist_name if tracks and tracks[0].playlist_name else None
        self._emit(
            EngineEventType.PLAYLIST_STARTED,
            track_total=total,
            message=f"Starting playlist download for {total} track(s).",
        )

        for i, track in enumerate(tracks, 1):
            if self.controller:
                self.controller.wait_if_paused()
                if self.controller.is_cancelled():
                    if playlist_name and results:
                        self.organizer.write_playlist_manifest(
                            playlist_name,
                            [result.file_path for result in results if result.file_path],
                        )
                    self._emit(
                        EngineEventType.PLAYLIST_CANCELLED,
                        track_index=i,
                        track_total=total,
                        message="Playlist download cancelled.",
                    )
                    return results

            logger.info(f"[{i}/{total}] {track.artist_string} — {track.title}")
            self._emit(
                EngineEventType.TRACK_STARTED,
                track=track,
                track_index=i,
                track_total=total,
            )
            result = self.download_track(track, track_index=i, track_total=total)
            results.append(result)

        if playlist_name:
            self.organizer.write_playlist_manifest(
                playlist_name,
                [result.file_path for result in results if result.file_path],
            )

        self._emit(
            EngineEventType.PLAYLIST_COMPLETED,
            track_total=total,
            message=f"Processed {len(results)} track(s).",
        )
        return results
