import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from antra.core.models import DownloadStatus
from antra.core.service import AntraService, RuntimeOptions

from .config import TelegramConfig
from .library import LibraryIndex, normalize_query
from .models import TrackAsset


class PendingQueueFull(RuntimeError):
    pass


class MusicRequestError(RuntimeError):
    """An input problem that is safe to explain directly to the Telegram user."""


def _youtube_music_input_kind(value: str) -> str | None:
    """Classify strict YouTube Music URLs without trusting substring matches."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "music.youtube.com":
        return None
    if parsed.username or parsed.password:
        return None

    query = parse_qs(parsed.query)
    if parsed.path.rstrip("/") == "/watch" and query.get("v") and not query.get("list"):
        return "track"
    return "collection"


class MusicResolver:
    def __init__(
        self,
        config: TelegramConfig,
        library: LibraryIndex,
        service_factory: Callable[[], AntraService] = AntraService,
    ):
        self.config = config
        self.library = library
        self._service_factory = service_factory
        self._service: AntraService | None = None
        self._download_lock = threading.Lock()

    def resolve(self, query: str) -> TrackAsset | None:
        youtube_music_kind = _youtube_music_input_kind(query)
        if youtube_music_kind == "collection":
            raise MusicRequestError(
                "Пока отправьте ссылку на один трек YouTube Music, а не альбом или плейлист."
            )

        # In download mode, only a high-confidence local match may suppress a
        # provider lookup. This avoids returning another track by the same artist.
        local_threshold = 0.78 if self.config.resolve_mode == "download" else 0.42
        local = None
        if youtube_music_kind is None:
            local = self.library.find_best(query, min_score=local_threshold)
        if local is not None or self.config.resolve_mode == "library":
            return local

        # Antra's organizer/state file is not safe for concurrent writers. Local
        # library searches may run in parallel, but provider downloads are serial.
        with self._download_lock:
            if self._service is None:
                self._service = self._service_factory()
            options = RuntimeOptions(
                output_dir=str(self.config.library_dir),
                output_format=self.config.download_format,
            )
            if youtube_music_kind == "track":
                results = self._service.download_playlist(query, options=options)
            else:
                track = self._service.search_track(query, options=options)
                if track is None:
                    return None
                results = self._service.download_tracks([track], options=options)
        for result in results:
            if (
                result.status in {DownloadStatus.COMPLETED, DownloadStatus.SKIPPED}
                and result.file_path
            ):
                path = Path(result.file_path).expanduser().resolve()
                asset = self.library.add_path(path)
                return TrackAsset(
                    path=asset.path,
                    title=result.track.title or asset.title,
                    artist=result.track.artist_string or asset.artist,
                    album=result.track.album or asset.album,
                    duration_seconds=result.track.duration_seconds or asset.duration_seconds,
                    source=result.source_used or "antra",
                )
        return None


class JobCoordinator:
    """Bounds blocking work and coalesces concurrent copies of the same query."""

    def __init__(self, resolver: MusicResolver, max_concurrent: int, max_pending: int):
        self.resolver = resolver
        self.max_pending = max_pending
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[TrackAsset | None]] = {}

    async def resolve(self, query: str) -> TrackAsset | None:
        key = normalize_query(query)
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= self.max_pending:
                    raise PendingQueueFull("too many pending music requests")
                task = asyncio.create_task(self._run(query))
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _run(self, query: str) -> TrackAsset | None:
        async with self._semaphore:
            return await asyncio.to_thread(self.resolver.resolve, query)
