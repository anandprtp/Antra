import asyncio
import hashlib
import math
import mimetypes
import tempfile
from pathlib import Path

from telegram import Bot

from .models import TrackAsset
from .storage_db import StorageCatalog, StoredPart


CLOUD_MAX_PART_BYTES = 19_000_000


class TelegramStorageError(RuntimeError):
    pass


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
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def archive(
        self,
        bot: Bot,
        asset: TrackAsset,
        *,
        track_id: str,
        chat_id: int,
    ) -> bool:
        lock = await self._lock_for(track_id)
        async with lock:
            path = asset.path.expanduser().resolve(strict=True)
            total_bytes = path.stat().st_size
            if total_bytes <= 0:
                raise TelegramStorageError("cannot archive an empty media file")
            whole_sha = await asyncio.to_thread(sha256_file, path)
            if await asyncio.to_thread(self.catalog.is_ready, track_id, whole_sha):
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
            except Exception as exc:
                await asyncio.to_thread(
                    self.catalog.mark_failed,
                    track_id,
                    str(exc),
                )
                raise
            return True

    async def _lock_for(self, track_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(track_id, asyncio.Lock())
