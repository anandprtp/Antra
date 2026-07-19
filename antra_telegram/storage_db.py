import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import TrackAsset


CREATE_TRACKS_TABLE = """
CREATE TABLE IF NOT EXISTS stored_tracks (
    track_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    duration_seconds REAL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    part_count INTEGER NOT NULL,
    part_bytes INTEGER NOT NULL DEFAULT 18000000,
    layout_version INTEGER NOT NULL DEFAULT 1,
    storage_chat_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('uploading', 'ready', 'failed')),
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

CREATE_PARTS_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_parts (
    track_id TEXT NOT NULL REFERENCES stored_tracks(track_id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (track_id, part_index),
    UNIQUE (chat_id, message_id)
)
"""


@dataclass(frozen=True)
class StoredPart:
    track_id: str
    part_index: int
    byte_offset: int
    byte_length: int
    sha256: str
    chat_id: int
    message_id: int
    file_id: str
    file_unique_id: str | None


@dataclass(frozen=True)
class StoredTrack:
    track_id: str
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    filename: str
    mime_type: str
    total_bytes: int
    sha256: str
    part_count: int
    part_bytes: int
    storage_chat_id: int


class StorageCatalog:
    """Durable catalog for Telegram-backed music objects.

    Telegram itself cannot enumerate a bot's storage history. The local catalog
    is therefore part of the storage protocol, not merely a performance cache.
    """

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self._ensure_schema()

    def is_ready(
        self,
        track_id: str,
        sha256: str,
        *,
        part_bytes: int | None = None,
        total_bytes: int | None = None,
    ) -> bool:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT part_count, sha256, part_bytes, total_bytes
                FROM stored_tracks
                WHERE track_id = ? AND state = 'ready'
                """,
                (track_id,),
            ).fetchone()
            if row is None or str(row[1]) != sha256:
                return False
            if part_bytes is not None and int(row[2]) != part_bytes:
                return False
            if total_bytes is not None and int(row[3]) != total_bytes:
                return False
            stored_parts = int(
                db.execute(
                    "SELECT COUNT(*) FROM telegram_parts WHERE track_id = ?",
                    (track_id,),
                ).fetchone()[0]
            )
            return stored_parts == int(row[0])

    def begin_upload(
        self,
        *,
        track_id: str,
        asset: TrackAsset,
        mime_type: str,
        total_bytes: int,
        sha256: str,
        part_count: int,
        part_bytes: int,
        storage_chat_id: int,
    ) -> None:
        now = int(time.time())
        with self._connection() as db:
            existing = db.execute(
                """
                SELECT sha256, total_bytes, part_count, part_bytes,
                       storage_chat_id
                FROM stored_tracks
                WHERE track_id = ?
                """,
                (track_id,),
            ).fetchone()
            layout = (
                sha256,
                total_bytes,
                part_count,
                part_bytes,
                storage_chat_id,
            )
            if existing is not None and tuple(existing) != layout:
                db.execute(
                    "DELETE FROM telegram_parts WHERE track_id = ?",
                    (track_id,),
                )
            db.execute(
                """
                INSERT INTO stored_tracks(
                    track_id, title, artist, album, duration_seconds, filename,
                    mime_type, total_bytes, sha256, part_count, storage_chat_id,
                    part_bytes, layout_version, state, last_error, created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'uploading', NULL, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    album = excluded.album,
                    duration_seconds = excluded.duration_seconds,
                    filename = excluded.filename,
                    mime_type = excluded.mime_type,
                    total_bytes = excluded.total_bytes,
                    sha256 = excluded.sha256,
                    part_count = excluded.part_count,
                    part_bytes = excluded.part_bytes,
                    layout_version = excluded.layout_version,
                    storage_chat_id = excluded.storage_chat_id,
                    state = 'uploading',
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    track_id,
                    asset.title,
                    asset.artist,
                    asset.album,
                    asset.duration_seconds,
                    asset.path.name,
                    mime_type,
                    total_bytes,
                    sha256,
                    part_count,
                    storage_chat_id,
                    part_bytes,
                    now,
                    now,
                ),
            )
        self._protect_file()

    def existing_part_indices(self, track_id: str) -> set[int]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT part_index FROM telegram_parts WHERE track_id = ?",
                (track_id,),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def record_part(self, part: StoredPart) -> None:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO telegram_parts(
                    track_id, part_index, byte_offset, byte_length, sha256,
                    chat_id, message_id, file_id, file_unique_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id, part_index) DO UPDATE SET
                    byte_offset = excluded.byte_offset,
                    byte_length = excluded.byte_length,
                    sha256 = excluded.sha256,
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    file_id = excluded.file_id,
                    file_unique_id = excluded.file_unique_id,
                    created_at = excluded.created_at
                """,
                (
                    part.track_id,
                    part.part_index,
                    part.byte_offset,
                    part.byte_length,
                    part.sha256,
                    part.chat_id,
                    part.message_id,
                    part.file_id,
                    part.file_unique_id,
                    int(time.time()),
                ),
            )
        self._protect_file()

    def mark_ready(self, track_id: str) -> None:
        with self._connection() as db:
            expected_row = db.execute(
                "SELECT part_count FROM stored_tracks WHERE track_id = ?",
                (track_id,),
            ).fetchone()
            actual = int(
                db.execute(
                    "SELECT COUNT(*) FROM telegram_parts WHERE track_id = ?",
                    (track_id,),
                ).fetchone()[0]
            )
            if expected_row is None or actual != int(expected_row[0]):
                raise RuntimeError("Telegram storage upload is incomplete")
            db.execute(
                """
                UPDATE stored_tracks
                SET state = 'ready', last_error = NULL, updated_at = ?
                WHERE track_id = ?
                """,
                (int(time.time()), track_id),
            )

    def mark_failed(self, track_id: str, error: str) -> None:
        with self._connection() as db:
            db.execute(
                """
                UPDATE stored_tracks
                SET state = 'failed', last_error = ?, updated_at = ?
                WHERE track_id = ?
                """,
                ((error or "unknown storage error")[:1000], int(time.time()), track_id),
            )

    def parts_for(self, track_id: str) -> list[StoredPart]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT track_id, part_index, byte_offset, byte_length, sha256,
                       chat_id, message_id, file_id, file_unique_id
                FROM telegram_parts
                WHERE track_id = ?
                ORDER BY part_index
                """,
                (track_id,),
            ).fetchall()
        return [StoredPart(*row) for row in rows]

    def ready_track(self, track_id: str) -> StoredTrack | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT track_id, title, artist, album, duration_seconds,
                       filename, mime_type, total_bytes, sha256, part_count,
                       part_bytes, storage_chat_id
                FROM stored_tracks
                WHERE track_id = ? AND state = 'ready'
                """,
                (track_id,),
            ).fetchone()
        return StoredTrack(*row) if row else None

    def ready_tracks(self) -> list[StoredTrack]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT track_id, title, artist, album, duration_seconds,
                       filename, mime_type, total_bytes, sha256, part_count,
                       part_bytes, storage_chat_id
                FROM stored_tracks
                WHERE state = 'ready'
                ORDER BY artist, album, title, track_id
                """
            ).fetchall()
        return [StoredTrack(*row) for row in rows]

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(CREATE_TRACKS_TABLE)
            db.execute(CREATE_PARTS_TABLE)
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(stored_tracks)")
            }
            if "part_bytes" not in columns:
                db.execute(
                    "ALTER TABLE stored_tracks ADD COLUMN "
                    "part_bytes INTEGER NOT NULL DEFAULT 18000000"
                )
            if "layout_version" not in columns:
                db.execute(
                    "ALTER TABLE stored_tracks ADD COLUMN "
                    "layout_version INTEGER NOT NULL DEFAULT 1"
                )
        self._protect_file()

    def _connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _protect_file(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
