"""
Download engine — orchestrates resolve → download → tag → organize.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

from mutagen import File as MutagenFile

from antra.core.control import DownloadController
from antra.core.events import EngineEvent, EngineEventType
from antra.core.models import AudioFormat, TrackMetadata, DownloadResult, DownloadStatus
from antra.core.resolver import SourceResolver
from antra.sources.base import RateLimitedError
from antra.utils.lyrics import LyricsFetcher
from antra.utils.organizer import LibraryOrganizer
from antra.utils.tagger import FileTagger
from antra.utils.transcoder import AudioTranscoder

logger = logging.getLogger(__name__)

# errno values that indicate the output filesystem is no longer accessible
# (NAS disconnected, drive ejected, SMB session dropped after sleep, etc.)
_MOUNT_LOST_ERRNOS = frozenset({
    13,   # EACCES / EPERM  — permission denied (SMB session dropped)
    57,   # ENOTCONN        — socket not connected (macOS SMB after sleep)
    5,    # EIO             — I/O error (drive I/O failure)
    30,   # EROFS           — read-only filesystem (mount degraded)
    116,  # ESTALE          — stale NFS/SMB file handle
})


def _is_mount_lost_error(exc: BaseException) -> bool:
    """Return True if the exception looks like the output filesystem vanished."""
    return isinstance(exc, OSError) and exc.errno in _MOUNT_LOST_ERRNOS


@dataclass
class EngineConfig:
    max_retries: int = 3
    retry_delay: float = 5.0
    fetch_lyrics: bool = True
    fetch_artwork: bool = True
    output_format: str = "source"
    max_workers: int = 2


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
        self._emit_lock = threading.Lock()
        # Set when a mount-loss error is detected mid-batch so remaining workers
        # can abort immediately instead of producing per-track error messages.
        self._output_lost = threading.Event()
        self._output_lost_message: str = ""

    def _signal_output_lost(self, exc: OSError) -> None:
        """Record the first mount-loss error so workers can abort fast."""
        if not self._output_lost.is_set():
            self._output_lost_message = (
                f"Output directory became inaccessible mid-download "
                f"(errno {exc.errno}: {exc.strerror}). "
                "This usually means a NAS/network drive disconnected (e.g. Mac sleep). "
                "Remaining tracks skipped — re-queue to resume."
            )
            logger.error(f"  [MOUNT LOST]  {self._output_lost_message}")
            self._output_lost.set()

    def _emit(self, event_type: EngineEventType, **kwargs):
        if not self.event_callback:
            return
        with self._emit_lock:
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
    def _enrich_genres_if_needed(track: TrackMetadata) -> None:
        """Populate track.genres from MusicBrainz when Spotify didn't provide any."""
        if track.genres or not track.isrc:
            return
        try:
            from antra.utils.musicbrainz import fetch_genres
            genres = fetch_genres(track.isrc)
            if genres:
                track.genres = genres
                logger.debug(f"  [MB]  Genres for '{track.title}': {', '.join(genres)}")
        except Exception as e:
            logger.debug(f"  [MB]  Genre fetch failed: {e}")

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
        return self.cfg.output_format in {"flac", "lossless", "alac"}

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

        # Original duration-based check (unchanged)
        if (
            actual_seconds < expected_seconds * 0.8
            and (expected_seconds - actual_seconds) >= 20
        ):
            return True

        # Secondary file-size check for FLAC files.
        # FLAC headers declare total samples upfront, so Mutagen reports the
        # *intended* duration even when the stream was cut short mid-download.
        # This catches those cases by comparing actual file size against a
        # conservative minimum estimate derived from the header's own
        # bit_depth / sample_rate.
        if cls._is_truncated_flac_by_size(file_path):
            return True

        return False

    @staticmethod
    def _is_truncated_flac_by_size(file_path: str) -> bool:
        """
        Detect truncated FLAC downloads by comparing actual file size against
        the minimum expected size based on the FLAC header's own metadata.

        FLAC headers write the total sample count up front, so Mutagen
        reports the full *intended* duration even when the file was truncated
        mid-stream.  This check catches those cases.

        Only runs on .flac files.  Uses a very conservative ratio threshold
        of 0.25 — real FLAC files never compress below ~0.35x of raw PCM,
        while truncated files typically show 0.15–0.26x.
        """
        if not file_path.lower().endswith(".flac"):
            return False

        try:
            from mutagen.flac import FLAC as FLACFile

            audio = FLACFile(file_path)
            if not audio or not audio.info:
                return False

            bits = getattr(audio.info, "bits_per_sample", None)
            rate = getattr(audio.info, "sample_rate", None)
            channels = getattr(audio.info, "channels", None)
            length = getattr(audio.info, "length", None)

            if not all((bits, rate, channels, length)):
                return False
            if length < 60:
                return False  # Don't flag short tracks

            actual_size = os.path.getsize(file_path)
            # Raw PCM size for the declared duration
            raw_pcm_bytes = length * rate * channels * (bits / 8)
            # FLAC typically compresses to 50-70% of raw.
            # Use 0.25 as our floor — anything below this is definitely truncated.
            # (Even the most silence-heavy FLAC only compresses to ~35%.)
            min_expected_bytes = raw_pcm_bytes * 0.25

            if actual_size < min_expected_bytes:
                ratio = actual_size / raw_pcm_bytes if raw_pcm_bytes > 0 else 0
                logger.debug(
                    f"[Engine] FLAC size check: {file_path} is {actual_size / (1024*1024):.1f}MB "
                    f"but expected ≥{min_expected_bytes / (1024*1024):.1f}MB "
                    f"(ratio={ratio:.2f}, {bits}bit/{rate}Hz/{length:.0f}s)"
                )
                return True

        except Exception as e:
            logger.debug(f"[Engine] FLAC size check failed: {e}")

        return False

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

        # 1. Resume check — only skip if the existing file meets the current output format.
        existing = self.organizer.is_already_downloaded(track)
        if existing:
            # In lossless-only mode, don't accept a previously-downloaded lossy file.
            # Re-download it as lossless instead.
            if self._requires_lossless_output():
                ext = os.path.splitext(existing)[1].lower()
                lossy_extensions = {".mp3", ".aac", ".m4a"}
                # .m4a could be ALAC (lossless) — check the actual codec
                if ext in lossy_extensions:
                    is_lossy_file = True
                    if ext == ".m4a":
                        try:
                            from mutagen import File as _MF
                            _audio = _MF(existing)
                            _codec = str(getattr(getattr(_audio, "info", None), "codec", "") or "").lower()
                            # alac codec = lossless; mp4a = AAC = lossy
                            is_lossy_file = "alac" not in _codec
                        except Exception:
                            pass  # can't probe → assume lossy, re-download
                    if is_lossy_file:
                        logger.info(
                            f"  [REDOWNLOAD]  '{track.title}' exists as lossy {ext} "
                            f"but lossless mode is active — re-downloading as lossless."
                        )
                        existing = None  # fall through to download

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
        # Adapters that were rate-limited get a second chance after all other
        # sources are exhausted (rate limit may have cleared by then).
        rate_limited_adapters: set[str] = set()
        # Once an adapter has been given its second chance, permanently exclude it.
        rate_limited_retried: set[str] = set()
        last_error: Optional[str] = None
        last_source: Optional[str] = None
        used_lossy_fallback: bool = False  # flag for post-download warning

        while True:
            # 3. Resolve — skip both permanently-excluded and currently rate-limited adapters.
            all_excluded = excluded_adapters | rate_limited_adapters
            resolution = self.resolver.resolve(track, excluded_adapters=all_excluded)
            if not resolution:
                # Before giving up: if any adapters were rate-limited and haven't
                # had their one retry yet, unblock them and try again.
                newly_retryable = rate_limited_adapters - rate_limited_retried
                if newly_retryable:
                    logger.info(
                        f"  [RATE]  All other sources exhausted — retrying rate-limited: "
                        f"{', '.join(newly_retryable)}"
                    )
                    rate_limited_retried |= newly_retryable
                    rate_limited_adapters.clear()
                    continue

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
            # Track if we ended up using a lossy source in lossless-prefer mode
            # (so we can emit a post-download warning). The resolver already handles
            # the "prefer lossless, fall back to lossy as last resort" logic.
            if self._requires_lossless_output() and not result.is_lossless:
                used_lossy_fallback = True
            self._hydrate_track_metadata(track, result)
            adapter.hydrate_track_metadata(track, result)
            self._fetch_lyrics_if_needed(track)
            # Layout must use post-hydration metadata (album/year from the resolver, etc.)
            try:
                output_base = self.organizer.get_output_path(track)
            except OSError as e:
                if _is_mount_lost_error(e):
                    self._signal_output_lost(e)
                raise
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

                    if attempt == 1:
                        logger.info(
                            f"  \U0001f4e5 [Downloading] [{track_index}/{track_total}] {track.title} by {track.artist_string} ({source_quality})"
                        )
                    else:
                        logger.info(
                            f"  \U0001f501 [Retry {attempt}] [{track_index}/{track_total}] {track.title} ({source_quality})"
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
                    if _is_mount_lost_error(e):
                        self._signal_output_lost(e)
                    final_error = e
                    last_error = str(e)
                    last_source = adapter.name
                    adapter.mark_failed_result(result, e)

                    # Rate-limited: skip to next source immediately — no sleep, no retry.
                    if isinstance(e, RateLimitedError):
                        logger.info(f"  [RATE]  {adapter.name} rate-limited — falling back to next source immediately")
                        if adapter.name in rate_limited_retried:
                            # Already gave this adapter its one retry — permanently exclude.
                            excluded_adapters.add(adapter.name)
                        else:
                            # Defer for a possible second chance after other sources are tried.
                            rate_limited_adapters.add(adapter.name)
                        break

                    will_retry = attempt < self.cfg.max_retries and adapter.should_retry_download(result, e)
                    if adapter.name == "hifi" and "all quality levels failed" in str(e).lower():
                        logger.info("  [INFO]  HiFi mirrors could not provide a valid stream. Trying next source...")
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
                # 4. Enrich genres + tag
                self._enrich_genres_if_needed(track)
                logger.debug(
                    "  [TAG]  %s | album=%r artwork=%s lyrics=%s synced=%s genres=%s",
                    file_path,
                    track.album,
                    bool(track.artwork_url),
                    bool(track.lyrics),
                    bool(track.synced_lyrics),
                    track.genres or [],
                )
                tag_ok = self.tagger.tag(file_path, track)
                if not tag_ok:
                    logger.warning(
                        f"  [WARN]  Metadata tagging did not complete for {file_path}. "
                        "This usually means the output container is unsupported for embedded tags."
                    )

                # 5. Mark done
                self.organizer.mark_downloaded(track, file_path)

                size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
                attempt_time = getattr(self, "_last_attempt_start", time.time())
                elapsed = time.time() - attempt_time
                
                logger.info(
                    f"  \u2728 [Complete] [{track_index}/{track_total}] {track.title} by {track.artist_string}"
                )
                if used_lossy_fallback:
                    logger.warning(
                        f"  \u26a0\ufe0f  [{track.title}] No lossless source available — "
                        f"downloaded as {result.quality_label} from {adapter.name}. "
                        f"Not true lossless."
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

            # Rate-limited adapters already placed in rate_limited_adapters above — skip
            # the regular exclude logic so they don't also land in excluded_adapters.
            if isinstance(final_error, RateLimitedError):
                continue

            # Truncated downloads: the adapter found the track but the stream ended early
            # (network blip, proxy cut it off). Don't permanently exclude — instead:
            # 1. Mark the adapter as globally rate-limited in the resolver (120s cooldown)
            #    so ALL parallel workers immediately start preferring other adapters.
            #    Without this, workers running in parallel each independently queue on the
            #    broken adapter, discovering the truncation one at a time.
            # 2. Defer the adapter to the end of this track's queue (rate_limited_adapters)
            #    so Amazon/DAB get a fair shot first; the adapter gets one last retry if
            #    nothing else works (useful when the adapter is the only one that can find
            #    the track, e.g. featured-artist titles that defeat DAB/Amazon search).
            if final_error is not None and "appears truncated" in str(final_error):
                # Signal all parallel workers to stop queuing on this adapter.
                self.resolver._mark_rate_limited(adapter.name, cooldown_seconds=120)

                if adapter.name in rate_limited_retried:
                    # Already had its second chance and still truncated — give up.
                    excluded_adapters.add(adapter.name)
                    logger.info(f"  [NEXT]  {adapter.name} truncated on second attempt — no more retries")
                else:
                    logger.info(
                        f"  [TRUNC]  {adapter.name} truncated — trying other sources first, "
                        f"will retry {adapter.name} as last resort if nothing else works"
                    )
                    rate_limited_adapters.add(adapter.name)
                continue

            should_exclude = True
            if final_error is not None:
                should_exclude = adapter.should_exclude_adapter_after_failure(result, final_error)

            if should_exclude:
                excluded_adapters.add(adapter.name)
                logger.info(f"  [NEXT]  {adapter.name} failed after retries, trying next source...")
            else:
                logger.info(f"  [NEXT]  {adapter.name} candidate failed, trying another match from the same source...")

    def download_playlist(self, tracks: list[TrackMetadata]) -> list[DownloadResult]:
        """Download all tracks in a playlist in parallel, returning results in original order."""
        total = len(tracks)
        playlist_name = tracks[0].playlist_name if tracks and tracks[0].playlist_name else None
        self._emit(
            EngineEventType.PLAYLIST_STARTED,
            track_total=total,
            message=f"Starting playlist download for {total} track(s).",
        )

        # results[i] will hold the DownloadResult for tracks[i]
        results: list[Optional[DownloadResult]] = [None] * total

        def _worker(index: int, track: TrackMetadata) -> tuple[int, DownloadResult]:
            # Abort immediately if the output filesystem was lost by a previous worker.
            if self._output_lost.is_set():
                return index, DownloadResult(
                    track=track,
                    status=DownloadStatus.FAILED,
                    error=self._output_lost_message,
                )
            if self.controller:
                self.controller.wait_if_paused()
                if self.controller.is_cancelled():
                    return index, DownloadResult(
                        track=track,
                        status=DownloadStatus.CANCELLED,
                        error="Cancelled",
                    )
            logger.info(f"[{index + 1}/{total}] {track.artist_string} — {track.title}")
            self._emit(
                EngineEventType.TRACK_STARTED,
                track=track,
                track_index=index + 1,
                track_total=total,
            )
            return index, self.download_track(track, track_index=index + 1, track_total=total)

        workers = max(1, self.cfg.max_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, i, track): i for i, track in enumerate(tracks)}
            for future in as_completed(futures):
                if self.controller and self.controller.is_cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    idx, result = future.result()
                    results[idx] = result
                except OSError as e:
                    idx = futures[future]
                    if _is_mount_lost_error(e):
                        self._signal_output_lost(e)
                    results[idx] = DownloadResult(
                        track=tracks[idx],
                        status=DownloadStatus.FAILED,
                        error=self._output_lost_message if self._output_lost.is_set() else str(e),
                    )
                except Exception as e:
                    idx = futures[future]
                    logger.warning(f"Worker for track {idx + 1} raised unexpectedly: {e}")
                    results[idx] = DownloadResult(
                        track=tracks[idx],
                        status=DownloadStatus.FAILED,
                        error=str(e),
                    )

        # Fill any slots that were cancelled or never completed
        final: list[DownloadResult] = []
        for i, r in enumerate(results):
            if r is None:
                r = DownloadResult(
                    track=tracks[i],
                    status=DownloadStatus.CANCELLED,
                    error="Cancelled",
                )
            final.append(r)

        if self.controller and self.controller.is_cancelled():
            if playlist_name and final:
                self.organizer.write_playlist_manifest(
                    playlist_name,
                    [r.file_path for r in final if r.file_path],
                )
            self._emit(
                EngineEventType.PLAYLIST_CANCELLED,
                track_total=total,
                message="Playlist download cancelled.",
            )
            return final

        if playlist_name:
            self.organizer.write_playlist_manifest(
                playlist_name,
                [r.file_path for r in final if r.file_path],
            )

        self._emit(
            EngineEventType.PLAYLIST_COMPLETED,
            track_total=total,
            message=f"Processed {len(final)} track(s).",
        )

        # If mount loss was detected, raise so json_cli surfaces the error in
        # the playlist_summary and subsequent URLs are also skipped cleanly.
        if self._output_lost.is_set():
            raise OSError(self._output_lost_message)

        return final
