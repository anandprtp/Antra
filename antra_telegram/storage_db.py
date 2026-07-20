import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TrackAsset


CATALOG_FORMAT = "antra.telegram-storage-catalog"
CATALOG_VERSION = 1
MAX_CATALOG_BYTES = 10 * 1024 * 1024
MAX_CATALOG_TRACKS = 10_000
MAX_CATALOG_PARTS = 50_000
MAX_STORAGE_PART_BYTES = 19_000_000


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


@dataclass(frozen=True)
class CatalogImportResult:
    imported: int
    skipped: int


@dataclass(frozen=True)
class _ManifestTrack:
    track: StoredTrack
    parts: tuple[StoredPart, ...]


class StorageCatalogBackupError(ValueError):
    """A storage-catalog backup is invalid, incompatible, or conflicting."""


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

    def mark_corrupt(self, track_id: str, error: str) -> None:
        """Invalidate damaged cloud parts so a later archive uploads clean bytes."""
        with self._connection() as db:
            db.execute(
                "DELETE FROM telegram_parts WHERE track_id = ?",
                (track_id,),
            )
            db.execute(
                """
                UPDATE stored_tracks
                SET state = 'failed', last_error = ?, updated_at = ?
                WHERE track_id = ?
                """,
                (
                    (error or "corrupt Telegram storage object")[:1000],
                    int(time.time()),
                    track_id,
                ),
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

    def export_manifest(
        self,
        bot_id: int,
        *,
        now: int | None = None,
    ) -> bytes:
        self._validate_positive_int(bot_id, "bot_id")
        with self._connection() as db:
            db.execute("BEGIN")
            track_rows = db.execute(
                """
                SELECT track_id, title, artist, album, duration_seconds,
                       filename, mime_type, total_bytes, sha256, part_count,
                       part_bytes, storage_chat_id
                FROM stored_tracks
                WHERE state = 'ready'
                ORDER BY artist, album, title, track_id
                """
            ).fetchall()
            tracks: list[dict[str, Any]] = []
            for row in track_rows:
                track = StoredTrack(*row)
                part_rows = db.execute(
                    """
                    SELECT track_id, part_index, byte_offset, byte_length,
                           sha256, chat_id, message_id, file_id,
                           file_unique_id
                    FROM telegram_parts
                    WHERE track_id = ?
                    ORDER BY part_index
                    """,
                    (track.track_id,),
                ).fetchall()
                parts = [StoredPart(*part_row) for part_row in part_rows]
                tracks.append(
                    {
                        "track_id": track.track_id,
                        "title": track.title,
                        "artist": track.artist,
                        "album": track.album,
                        "duration_seconds": track.duration_seconds,
                        "filename": track.filename,
                        "mime_type": track.mime_type,
                        "total_bytes": track.total_bytes,
                        "sha256": track.sha256,
                        "part_count": track.part_count,
                        "part_bytes": track.part_bytes,
                        "layout_version": 1,
                        "storage_chat_id": track.storage_chat_id,
                        "parts": [
                            {
                                "part_index": part.part_index,
                                "byte_offset": part.byte_offset,
                                "byte_length": part.byte_length,
                                "sha256": part.sha256,
                                "chat_id": part.chat_id,
                                "message_id": part.message_id,
                                "file_id": part.file_id,
                                "file_unique_id": part.file_unique_id,
                            }
                            for part in parts
                        ],
                    }
                )
            db.execute("COMMIT")

        manifest = {
            "format": CATALOG_FORMAT,
            "version": CATALOG_VERSION,
            "created_at": int(time.time()) if now is None else now,
            "bot_id": bot_id,
            "tracks": tracks,
        }
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._parse_manifest(payload, expected_bot_id=bot_id)
        return payload

    def manifest_track_ids(
        self,
        payload: bytes | bytearray,
        *,
        expected_bot_id: int,
    ) -> tuple[str, ...]:
        tracks = self._parse_manifest(
            payload,
            expected_bot_id=expected_bot_id,
        )
        return tuple(track.track.track_id for track in tracks)

    def import_manifest(
        self,
        payload: bytes | bytearray,
        *,
        expected_bot_id: int,
    ) -> CatalogImportResult:
        tracks = self._parse_manifest(
            payload,
            expected_bot_id=expected_bot_id,
        )
        imported = 0
        skipped = 0
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for entry in tracks:
                    track = entry.track
                    existing = db.execute(
                        """
                        SELECT state, sha256, total_bytes, part_count,
                               part_bytes, storage_chat_id
                        FROM stored_tracks
                        WHERE track_id = ?
                        """,
                        (track.track_id,),
                    ).fetchone()
                    if existing is not None and str(existing[0]) == "ready":
                        existing_layout = (
                            str(existing[1]),
                            int(existing[2]),
                            int(existing[3]),
                            int(existing[4]),
                            int(existing[5]),
                        )
                        imported_layout = (
                            track.sha256,
                            track.total_bytes,
                            track.part_count,
                            track.part_bytes,
                            track.storage_chat_id,
                        )
                        if existing_layout != imported_layout:
                            raise StorageCatalogBackupError(
                                f"ready track {track.track_id} conflicts with backup"
                            )
                        existing_parts = int(
                            db.execute(
                                """
                                SELECT COUNT(*) FROM telegram_parts
                                WHERE track_id = ?
                                """,
                                (track.track_id,),
                            ).fetchone()[0]
                        )
                        if existing_parts == track.part_count:
                            skipped += 1
                            continue

                    db.execute(
                        "DELETE FROM stored_tracks WHERE track_id = ?",
                        (track.track_id,),
                    )
                    current = int(time.time())
                    db.execute(
                        """
                        INSERT INTO stored_tracks(
                            track_id, title, artist, album, duration_seconds,
                            filename, mime_type, total_bytes, sha256,
                            part_count, part_bytes, layout_version,
                            storage_chat_id, state, last_error, created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?,
                                'ready', NULL, ?, ?)
                        """,
                        (
                            track.track_id,
                            track.title,
                            track.artist,
                            track.album,
                            track.duration_seconds,
                            track.filename,
                            track.mime_type,
                            track.total_bytes,
                            track.sha256,
                            track.part_count,
                            track.part_bytes,
                            track.storage_chat_id,
                            current,
                            current,
                        ),
                    )
                    for part in entry.parts:
                        db.execute(
                            """
                            INSERT INTO telegram_parts(
                                track_id, part_index, byte_offset, byte_length,
                                sha256, chat_id, message_id, file_id,
                                file_unique_id, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                current,
                            ),
                        )
                    imported += 1
                db.execute("COMMIT")
        except StorageCatalogBackupError:
            raise
        except sqlite3.IntegrityError as exc:
            raise StorageCatalogBackupError(
                "catalog backup conflicts with existing Telegram messages"
            ) from exc
        except sqlite3.Error as exc:
            raise StorageCatalogBackupError(
                "catalog backup could not be imported atomically"
            ) from exc
        self._protect_file()
        return CatalogImportResult(imported=imported, skipped=skipped)

    @classmethod
    def _parse_manifest(
        cls,
        payload: bytes | bytearray,
        *,
        expected_bot_id: int,
    ) -> tuple[_ManifestTrack, ...]:
        cls._validate_positive_int(expected_bot_id, "expected_bot_id")
        if not isinstance(payload, (bytes, bytearray)):
            raise StorageCatalogBackupError("catalog backup must be bytes")
        if not payload or len(payload) > MAX_CATALOG_BYTES:
            raise StorageCatalogBackupError("catalog backup size is invalid")
        try:
            manifest = json.loads(
                bytes(payload).decode("utf-8"),
                parse_constant=lambda value: cls._reject_json_constant(value),
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise StorageCatalogBackupError(
                "catalog backup is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(manifest, dict):
            raise StorageCatalogBackupError("catalog backup root must be an object")
        if (
            manifest.get("format") != CATALOG_FORMAT
            or manifest.get("version") != CATALOG_VERSION
        ):
            raise StorageCatalogBackupError(
                "catalog backup format or version is unsupported"
            )
        bot_id = cls._required_int(manifest, "bot_id", minimum=1)
        if bot_id != expected_bot_id:
            raise StorageCatalogBackupError(
                "catalog backup belongs to a different Telegram bot"
            )
        cls._required_int(manifest, "created_at", minimum=0)
        raw_tracks = manifest.get("tracks")
        if (
            not isinstance(raw_tracks, list)
            or len(raw_tracks) > MAX_CATALOG_TRACKS
        ):
            raise StorageCatalogBackupError("catalog track list is invalid")

        seen_tracks: set[str] = set()
        seen_messages: set[tuple[int, int]] = set()
        total_parts = 0
        parsed: list[_ManifestTrack] = []
        for raw_track in raw_tracks:
            if not isinstance(raw_track, dict):
                raise StorageCatalogBackupError("catalog track is invalid")
            track_id = cls._required_text(raw_track, "track_id", maximum=128)
            if track_id in seen_tracks:
                raise StorageCatalogBackupError("catalog has duplicate track IDs")
            seen_tracks.add(track_id)
            filename = cls._required_text(
                raw_track,
                "filename",
                maximum=255,
            )
            if (
                filename in {".", ".."}
                or Path(filename).name != filename
                or "/" in filename
                or "\\" in filename
            ):
                raise StorageCatalogBackupError("catalog filename is unsafe")
            duration = raw_track.get("duration_seconds")
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or float(duration) < 0
                or float(duration) > 31 * 24 * 60 * 60
            ):
                raise StorageCatalogBackupError("catalog duration is invalid")
            total_bytes = cls._required_int(
                raw_track,
                "total_bytes",
                minimum=1,
                maximum=100 * 1024**4,
            )
            part_bytes = cls._required_int(
                raw_track,
                "part_bytes",
                minimum=1,
                maximum=MAX_STORAGE_PART_BYTES,
            )
            part_count = cls._required_int(
                raw_track,
                "part_count",
                minimum=1,
                maximum=MAX_CATALOG_PARTS,
            )
            if part_count != math.ceil(total_bytes / part_bytes):
                raise StorageCatalogBackupError(
                    "catalog part count does not match object size"
                )
            if cls._required_int(
                raw_track,
                "layout_version",
                minimum=1,
            ) != 1:
                raise StorageCatalogBackupError(
                    "catalog track layout is unsupported"
                )
            storage_chat_id = cls._required_int(
                raw_track,
                "storage_chat_id",
                allow_negative=True,
            )
            if storage_chat_id == 0:
                raise StorageCatalogBackupError("catalog storage chat is invalid")
            whole_sha = cls._required_sha256(raw_track, "sha256")
            mime_type = cls._required_text(
                raw_track,
                "mime_type",
                maximum=255,
            )
            raw_parts = raw_track.get("parts")
            if not isinstance(raw_parts, list) or len(raw_parts) != part_count:
                raise StorageCatalogBackupError(
                    "catalog track has incomplete parts"
                )
            total_parts += len(raw_parts)
            if total_parts > MAX_CATALOG_PARTS:
                raise StorageCatalogBackupError("catalog has too many parts")

            parts: list[StoredPart] = []
            expected_offset = 0
            for expected_index, raw_part in enumerate(raw_parts):
                if not isinstance(raw_part, dict):
                    raise StorageCatalogBackupError("catalog part is invalid")
                part_index = cls._required_int(
                    raw_part,
                    "part_index",
                    minimum=0,
                )
                offset = cls._required_int(
                    raw_part,
                    "byte_offset",
                    minimum=0,
                )
                expected_length = min(
                    part_bytes,
                    total_bytes - expected_offset,
                )
                length = cls._required_int(
                    raw_part,
                    "byte_length",
                    minimum=1,
                    maximum=part_bytes,
                )
                if (
                    part_index != expected_index
                    or offset != expected_offset
                    or length != expected_length
                ):
                    raise StorageCatalogBackupError(
                        "catalog part layout has a gap, overlap, or wrong length"
                    )
                chat_id = cls._required_int(
                    raw_part,
                    "chat_id",
                    allow_negative=True,
                )
                if chat_id != storage_chat_id:
                    raise StorageCatalogBackupError(
                        "catalog part belongs to an unexpected chat"
                    )
                message_id = cls._required_int(
                    raw_part,
                    "message_id",
                    minimum=1,
                )
                message_key = (chat_id, message_id)
                if message_key in seen_messages:
                    raise StorageCatalogBackupError(
                        "catalog has duplicate Telegram messages"
                    )
                seen_messages.add(message_key)
                file_unique_id = raw_part.get("file_unique_id")
                if file_unique_id is not None and (
                    not isinstance(file_unique_id, str)
                    or not file_unique_id
                    or len(file_unique_id) > 512
                ):
                    raise StorageCatalogBackupError(
                        "catalog file_unique_id is invalid"
                    )
                parts.append(
                    StoredPart(
                        track_id=track_id,
                        part_index=part_index,
                        byte_offset=offset,
                        byte_length=length,
                        sha256=cls._required_sha256(raw_part, "sha256"),
                        chat_id=chat_id,
                        message_id=message_id,
                        file_id=cls._required_text(
                            raw_part,
                            "file_id",
                            maximum=512,
                        ),
                        file_unique_id=file_unique_id,
                    )
                )
                expected_offset += length
            if expected_offset != total_bytes:
                raise StorageCatalogBackupError(
                    "catalog parts do not cover the complete object"
                )
            parsed.append(
                _ManifestTrack(
                    track=StoredTrack(
                        track_id=track_id,
                        title=cls._required_text(
                            raw_track,
                            "title",
                            maximum=2000,
                            allow_empty=True,
                        ),
                        artist=cls._required_text(
                            raw_track,
                            "artist",
                            maximum=2000,
                            allow_empty=True,
                        ),
                        album=cls._required_text(
                            raw_track,
                            "album",
                            maximum=2000,
                            allow_empty=True,
                        ),
                        duration_seconds=(
                            None if duration is None else float(duration)
                        ),
                        filename=filename,
                        mime_type=mime_type,
                        total_bytes=total_bytes,
                        sha256=whole_sha,
                        part_count=part_count,
                        part_bytes=part_bytes,
                        storage_chat_id=storage_chat_id,
                    ),
                    parts=tuple(parts),
                )
            )
        return tuple(parsed)

    @staticmethod
    def _reject_json_constant(value: str):
        raise StorageCatalogBackupError(
            f"catalog contains unsupported JSON constant {value}"
        )

    @staticmethod
    def _validate_positive_int(value: int, name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > 2**63 - 1
        ):
            raise StorageCatalogBackupError(f"{name} must be a positive integer")

    @staticmethod
    def _required_int(
        source: dict[str, Any],
        name: str,
        *,
        minimum: int | None = None,
        maximum: int = 2**63 - 1,
        allow_negative: bool = False,
    ) -> int:
        value = source.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise StorageCatalogBackupError(f"catalog {name} is invalid")
        lower_bound = -(2**63) if allow_negative else 0
        if minimum is not None:
            lower_bound = minimum
        if value < lower_bound or value > maximum:
            raise StorageCatalogBackupError(f"catalog {name} is out of range")
        return value

    @staticmethod
    def _required_text(
        source: dict[str, Any],
        name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
    ) -> str:
        value = source.get(name)
        if (
            not isinstance(value, str)
            or (not allow_empty and not value)
            or len(value) > maximum
            or "\x00" in value
        ):
            raise StorageCatalogBackupError(f"catalog {name} is invalid")
        return value

    @classmethod
    def _required_sha256(
        cls,
        source: dict[str, Any],
        name: str,
    ) -> str:
        value = cls._required_text(source, name, maximum=64)
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise StorageCatalogBackupError(f"catalog {name} is not SHA-256")
        return value

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
