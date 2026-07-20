import asyncio
import copy
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from antra.core.models import DownloadResult, DownloadStatus, TrackMetadata
from antra.core.service import AntraService, RuntimeOptions
from antra.sources.youtube import YouTubeAdapter

from .config import TelegramConfig
from .library import LibraryIndex, normalize_query
from .models import PlaylistPreview, TrackAsset


class PendingQueueFull(RuntimeError):
    pass


class MusicRequestError(RuntimeError):
    """An input problem that is safe to explain directly to the Telegram user."""


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def youtube_music_input_kind(value: str) -> str | None:
    """Classify strict YouTube Music URLs without trusting substring matches."""
    parsed = urlparse(value.strip())
    if parsed.hostname != "music.youtube.com":
        return None
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return "invalid"

    query = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")
    video_ids = query.get("v") or []
    playlist_ids = query.get("list") or []
    if (
        path == "/watch"
        and len(video_ids) == 1
        and _YOUTUBE_ID_RE.fullmatch(video_ids[0])
    ):
        if playlist_ids:
            return "collection" if len(playlist_ids) == 1 else "invalid"
        return "track"
    if path == "/playlist" and len(playlist_ids) == 1 and _YOUTUBE_ID_RE.fullmatch(playlist_ids[0]):
        return "collection"
    if path.startswith("/browse/") and _YOUTUBE_ID_RE.fullmatch(path.removeprefix("/browse/")):
        return "collection"
    return "invalid"


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
        youtube_music_kind = youtube_music_input_kind(query)
        if youtube_music_kind == "collection":
            raise MusicRequestError(
                "Этот плейлист нужно сначала открыть как список с кнопками."
            )
        if youtube_music_kind == "invalid":
            raise MusicRequestError("Некорректная ссылка YouTube Music.")

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
            service = self._get_service()
            if youtube_music_kind == "track":
                metadata_options = self._runtime_options(exact_youtube=True)
                tracks = service.fetch_playlist_tracks(query, options=metadata_options)
                if len(tracks) != 1:
                    raise MusicRequestError(
                        "Не удалось получить данные трека по прямой ссылке."
                    )
                track = tracks[0]
                track.request_kind = "track"
                options = self._runtime_options(
                    track=track,
                    exact_youtube=True,
                )
                results = service.download_tracks([track], options=options)
            else:
                options = self._runtime_options()
                track = service.search_track(query, options=options)
                if track is None:
                    return None
                results = service.download_tracks(
                    [track],
                    options=self._runtime_options(
                        track=track,
                        exact_youtube=self.config.fast_mode,
                    ),
                )
        return self._first_asset(results)

    def preview_playlist(self, url: str) -> PlaylistPreview:
        if youtube_music_input_kind(url) != "collection":
            raise MusicRequestError("Нужна корректная ссылка на плейлист YouTube Music.")
        with self._download_lock:
            tracks = self._get_service().fetch_playlist_tracks(
                url,
                options=self._runtime_options(),
            )
        if not tracks:
            raise MusicRequestError("Плейлист пуст или недоступен.")
        if len(tracks) > self.config.max_playlist_tracks:
            raise MusicRequestError(
                f"В плейлисте {len(tracks)} треков; максимум для бота — "
                f"{self.config.max_playlist_tracks}."
            )
        name = tracks[0].playlist_name or tracks[0].album or "YouTube Music"
        return PlaylistPreview(url, name, tuple(tracks))

    def resolve_track(self, track: TrackMetadata) -> TrackAsset | None:
        query = f"{track.artist_string} {track.title}".strip()
        local = self.library.find_best(query, min_score=0.78)
        if local is not None or self.config.resolve_mode == "library":
            return local

        selected = copy.deepcopy(track)
        selected.request_kind = "track"
        selected.playlist_name = None
        selected.playlist_owner = None
        selected.playlist_description = None
        selected.playlist_position = None
        with self._download_lock:
            results = self._get_service().download_tracks(
                [selected],
                options=self._runtime_options(
                    track=selected,
                    exact_youtube=self._is_exact_youtube_track(selected),
                ),
            )
        return self._first_asset(results)

    def _runtime_options(
        self,
        *,
        track: TrackMetadata | None = None,
        exact_youtube: bool = False,
    ) -> RuntimeOptions:
        output_format = self.config.download_format
        if exact_youtube and track is not None and self._should_preserve_source(track):
            output_format = "source"
        return RuntimeOptions(
            output_dir=str(self.config.library_dir),
            output_format=output_format,
            source_preference="youtube" if exact_youtube else None,
            source_exclusive=exact_youtube,
        )

    def _should_preserve_source(self, track: TrackMetadata) -> bool:
        duration = track.duration_seconds
        if duration is None or duration <= 0:
            return False
        bitrate = {
            "mp3": 320_000,
            "aac": 320_000,
            "m4a": 256_000,
        }.get(self.config.download_format)
        if bitrate is None:
            return False
        predicted_bytes = duration * bitrate / 8
        return predicted_bytes >= self.config.max_upload_bytes * 0.85

    @staticmethod
    def _is_exact_youtube_track(track: TrackMetadata) -> bool:
        return (
            (track.source_service or "").lower() == "youtube"
            and YouTubeAdapter._is_direct_video_url((track.source_url or "").strip())
        )

    def _get_service(self) -> AntraService:
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    def _first_asset(self, results: list[DownloadResult]) -> TrackAsset | None:
        for result in results:
            if result.status not in {DownloadStatus.COMPLETED, DownloadStatus.SKIPPED}:
                continue
            if not result.file_path:
                continue
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
        if results:
            failures = [
                result.error_message
                for result in results
                if result.error_message
            ]
            if failures:
                logger_message = "; ".join(failures[:3])
                # Avoid leaking provider internals to Telegram while leaving a
                # useful exception chain for the application log.
                raise MusicRequestError(
                    "Источник найден, но аудио не удалось подготовить."
                ) from RuntimeError(logger_message)
            raise MusicRequestError(
                "Источник найден, но аудио не удалось подготовить."
            )
        return None


class JobCoordinator:
    """Bounds blocking work and coalesces concurrent copies of the same query."""

    def __init__(self, resolver: MusicResolver, max_concurrent: int, max_pending: int):
        self.resolver = resolver
        self.max_pending = max_pending
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def resolve(self, query: str) -> TrackAsset | None:
        return await self._submit(
            f"query:{normalize_query(query)}",
            lambda: self.resolver.resolve(query),
        )

    async def preview_playlist(self, url: str) -> PlaylistPreview:
        return await self._submit(
            f"preview:{url}",
            lambda: self.resolver.preview_playlist(url),
        )

    async def resolve_playlist_track(
        self,
        session_token: str,
        index: int,
        track: TrackMetadata,
    ) -> TrackAsset | None:
        return await self._submit(
            f"playlist-track:{session_token}:{index}",
            lambda: self.resolver.resolve_track(track),
        )

    async def _submit(self, key: str, blocking: Callable[[], Any]) -> Any:
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= self.max_pending:
                    raise PendingQueueFull("too many pending music requests")
                task = asyncio.create_task(self._run(blocking))
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed, task_key=key: self._schedule_cleanup(
                        task_key,
                        completed,
                    )
                )
        return await asyncio.shield(task)

    def _schedule_cleanup(self, key: str, task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            # The result is still delivered to active waiters. Reading the
            # exception here only prevents orphaned jobs from producing an
            # unhandled-task warning after every waiter was cancelled.
            pass
        try:
            asyncio.create_task(self._discard_completed(key, task))
        except RuntimeError:
            # The loop can already be closing during process shutdown.
            pass

    async def _discard_completed(
        self,
        key: str,
        task: asyncio.Task[Any],
    ) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _run(self, blocking: Callable[[], Any]) -> Any:
        async with self._semaphore:
            return await asyncio.to_thread(blocking)
