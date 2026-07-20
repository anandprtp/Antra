import asyncio
import hashlib
import math
import mimetypes
import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot
from telegram.error import NetworkError, RetryAfter

from .models import TrackAsset
from .storage_db import CatalogImportResult, StorageCatalog, StoredPart


CLOUD_MAX_PART_BYTES = 19_000_000


class TelegramStorageError(RuntimeError):
    pass


class TelegramStorageCorruptionError(TelegramStorageError):
    pass


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


def sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def copy_part(
    source: Path,
    destination: Path,
    *,
    offset: int,
    length: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    written = 0
    with source.open("rb") as source_handle, destination.open("wb") as target:
        source_handle.seek(offset)
        while written < length:
            chunk = source_handle.read(min(1024 * 1024, length - written))
            if not chunk:
                break
            target.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    if written != length:
        destination.unlink(missing_ok=True)
        raise TelegramStorageError("source file changed during Telegram upload")
    return written, digest.hexdigest()


class TelegramStorage:
    """Archives exact media bytes as restorable sub-20 MB Telegram documents."""

    def __init__(
        self,
        catalog: StorageCatalog,
        *,
        part_bytes: int = 18_000_000,
    ):
        if part_bytes <= 0 or part_bytes > CLOUD_MAX_PART_BYTES:
            raise ValueError(
                f"Telegram cloud storage parts must be between 1 and "
                f"{CLOUD_MAX_PART_BYTES} bytes"
            )
        self.catalog = catalog
        self.part_bytes = part_bytes
        self._locks: dict[str, _LockEntry] = {}
        self._locks_guard = asyncio.Lock()
        self._restore_semaphore = asyncio.Semaphore(2)

    async def archive(
        self,
        bot: Bot,
        asset: TrackAsset,
        *,
        track_id: str,
        chat_id: int,
    ) -> bool:
        async with self._track_lock(track_id):
            path = asset.path.expanduser().resolve(strict=True)
            total_bytes = path.stat().st_size
            if total_bytes <= 0:
                raise TelegramStorageError("cannot archive an empty media file")
            whole_sha = await asyncio.to_thread(sha256_file, path)
            if await asyncio.to_thread(
                self.catalog.is_ready,
                track_id,
                whole_sha,
                part_bytes=self.part_bytes,
                total_bytes=total_bytes,
            ):
                return False

            part_count = math.ceil(total_bytes / self.part_bytes)
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            await asyncio.to_thread(
                self.catalog.begin_upload,
                track_id=track_id,
                asset=asset,
                mime_type=mime_type,
                total_bytes=total_bytes,
                sha256=whole_sha,
                part_count=part_count,
                part_bytes=self.part_bytes,
                storage_chat_id=chat_id,
            )
            existing = await asyncio.to_thread(
                self.catalog.existing_part_indices,
                track_id,
            )
            try:
                with tempfile.TemporaryDirectory(
                    prefix="antra-telegram-storage-"
                ) as temp_dir:
                    for part_index in range(part_count):
                        if part_index in existing:
                            continue
                        offset = part_index * self.part_bytes
                        length = min(self.part_bytes, total_bytes - offset)
                        part_path = Path(temp_dir) / (
                            f"{track_id}.part-{part_index + 1:04d}-of-"
                            f"{part_count:04d}.bin"
                        )
                        written, part_sha = await asyncio.to_thread(
                            copy_part,
                            path,
                            part_path,
                            offset=offset,
                            length=length,
                        )
                        with part_path.open("rb") as handle:
                            for attempt in range(3):
                                try:
                                    message = await bot.send_document(
                                        chat_id=chat_id,
                                        document=handle,
                                        filename=part_path.name,
                                        caption=(
                                            f"#antra_part_v1 id={track_id} "
                                            f"part={part_index + 1}/{part_count} "
                                            f"sha256={part_sha}"
                                        ),
                                        disable_notification=True,
                                        read_timeout=300,
                                        write_timeout=300,
                                    )
                                    break
                                except RetryAfter as exc:
                                    if attempt >= 2:
                                        raise
                                    handle.seek(0)
                                    await asyncio.sleep(
                                        float(exc.retry_after) + 0.5
                                    )
                        document = message.document
                        if document is None:
                            raise TelegramStorageError(
                                "Telegram did not return document metadata"
                            )
                        await asyncio.to_thread(
                            self.catalog.record_part,
                            StoredPart(
                                track_id=track_id,
                                part_index=part_index,
                                byte_offset=offset,
                                byte_length=written,
                                sha256=part_sha,
                                chat_id=int(message.chat_id),
                                message_id=int(message.message_id),
                                file_id=document.file_id,
                                file_unique_id=document.file_unique_id,
                            ),
                        )
                await asyncio.to_thread(self.catalog.mark_ready, track_id)
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    self.catalog.mark_failed,
                    track_id,
                    "upload cancelled; safe to retry",
                )
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    self.catalog.mark_failed,
                    track_id,
                    str(exc),
                )
                raise
            return True

    async def restore(
        self,
        bot: Bot,
        *,
        track_id: str,
        destination: Path,
    ) -> TrackAsset:
        async with self._track_lock(track_id):
            stored = await asyncio.to_thread(
                self.catalog.ready_track,
                track_id,
            )
            if stored is None:
                raise TelegramStorageError("track is not ready in Telegram storage")
            destination = destination.expanduser().resolve()
            if destination.is_file():
                current_sha = await asyncio.to_thread(sha256_file, destination)
                if current_sha == stored.sha256:
                    return TrackAsset(
                        destination,
                        stored.title,
                        stored.artist,
                        stored.album,
                        stored.duration_seconds,
                        source="telegram",
                    )

            temporary_output: Path | None = None
            try:
                parts = await asyncio.to_thread(self.catalog.parts_for, track_id)
                self._validate_layout(
                    stored.total_bytes,
                    stored.part_count,
                    parts,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.name}.restore-",
                    delete=False,
                ) as output:
                    temporary_output = Path(output.name)
                    whole_digest = hashlib.sha256()
                    written = 0
                    with tempfile.TemporaryDirectory(
                        prefix="antra-telegram-restore-"
                    ) as temp_dir:
                        async with self._restore_semaphore:
                            for part in parts:
                                part_path = Path(temp_dir) / (
                                    f"{track_id}-{part.part_index:04d}.bin"
                                )
                                await self._download_part(
                                    bot,
                                    part,
                                    part_path,
                                )
                                payload = await asyncio.to_thread(
                                    part_path.read_bytes,
                                )
                                if (
                                    len(payload) != part.byte_length
                                    or hashlib.sha256(payload).hexdigest()
                                    != part.sha256
                                ):
                                    raise TelegramStorageCorruptionError(
                                        f"Telegram part {part.part_index} "
                                        "failed checksum"
                                    )
                                if part.byte_offset != written:
                                    raise TelegramStorageCorruptionError(
                                        "Telegram part layout has a gap or overlap"
                                    )
                                output.write(payload)
                                whole_digest.update(payload)
                                written += len(payload)
                    output.flush()
                    os.fsync(output.fileno())

                if (
                    written != stored.total_bytes
                    or whole_digest.hexdigest() != stored.sha256
                ):
                    raise TelegramStorageCorruptionError(
                        "restored Telegram object failed checksum"
                    )
                os.replace(temporary_output, destination)
                temporary_output = None
                return TrackAsset(
                    destination,
                    stored.title,
                    stored.artist,
                    stored.album,
                    stored.duration_seconds,
                    source="telegram",
                )
            except TelegramStorageCorruptionError as exc:
                await asyncio.to_thread(
                    self.catalog.mark_corrupt,
                    track_id,
                    str(exc),
                )
                raise
            finally:
                if temporary_output is not None:
                    temporary_output.unlink(missing_ok=True)

    async def import_catalog(
        self,
        payload: bytes | bytearray,
        *,
        expected_bot_id: int,
    ) -> CatalogImportResult:
        track_ids = await asyncio.to_thread(
            self.catalog.manifest_track_ids,
            payload,
            expected_bot_id=expected_bot_id,
        )
        async with AsyncExitStack() as locks:
            for track_id in sorted(track_ids):
                await locks.enter_async_context(self._track_lock(track_id))
            return await asyncio.to_thread(
                self.catalog.import_manifest,
                payload,
                expected_bot_id=expected_bot_id,
            )

    async def _download_part(
        self,
        bot: Bot,
        part: StoredPart,
        destination: Path,
    ) -> None:
        for attempt in range(3):
            destination.unlink(missing_ok=True)
            try:
                telegram_file = await bot.get_file(part.file_id)
                await telegram_file.download_to_drive(
                    custom_path=destination,
                )
                return
            except RetryAfter as exc:
                if attempt >= 2:
                    raise TelegramStorageError(
                        "Telegram storage restore is rate limited"
                    ) from exc
                retry_after = exc.retry_after
                if hasattr(retry_after, "total_seconds"):
                    retry_after = retry_after.total_seconds()
                await asyncio.sleep(max(0.0, float(retry_after)) + 0.25)
            except NetworkError as exc:
                if attempt >= 2:
                    raise TelegramStorageError(
                        "Telegram storage restore is temporarily unavailable"
                    ) from exc
                await asyncio.sleep(0.5 * (2**attempt))

    @staticmethod
    def _validate_layout(
        total_bytes: int,
        part_count: int,
        parts: list[StoredPart],
    ) -> None:
        if len(parts) != part_count:
            raise TelegramStorageCorruptionError(
                "Telegram storage object is incomplete"
            )
        expected_offset = 0
        for expected_index, part in enumerate(parts):
            if (
                part.part_index != expected_index
                or part.byte_offset != expected_offset
                or part.byte_length <= 0
            ):
                raise TelegramStorageCorruptionError(
                    "Telegram storage part layout is invalid"
                )
            expected_offset += part.byte_length
        if expected_offset != total_bytes:
            raise TelegramStorageCorruptionError(
                "Telegram storage object size is invalid"
            )

    @asynccontextmanager
    async def _track_lock(self, track_id: str):
        async with self._locks_guard:
            entry = self._locks.get(track_id)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._locks[track_id] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._locks.get(track_id) is entry:
                    self._locks.pop(track_id, None)
