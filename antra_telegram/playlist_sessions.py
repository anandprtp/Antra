import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import asdict, fields
from pathlib import Path

from antra.core.models import TrackMetadata

from .models import PlaylistPreview, PlaylistSession


logger = logging.getLogger(__name__)


CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS playlist_sessions (
    token_hash TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    source_url TEXT NOT NULL,
    name TEXT NOT NULL,
    track_count INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
)
"""

CREATE_TRACKS_TABLE = """
CREATE TABLE IF NOT EXISTS playlist_tracks (
    token_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY(token_hash, ordinal),
    FOREIGN KEY(token_hash) REFERENCES playlist_sessions(token_hash) ON DELETE CASCADE
)
"""

_TRACK_FIELDS = frozenset(field.name for field in fields(TrackMetadata))


class PlaylistSessionError(RuntimeError):
    """Playlist state could not be stored or safely recovered."""


class PlaylistTooLarge(PlaylistSessionError):
    pass


class PlaylistSessionStore:
    """Owner-bound, restart-safe storage for compact Telegram callback tokens."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int,
        max_tracks: int,
        max_sessions_per_user: int = 5,
    ):
        self.path = path.expanduser().resolve()
        self.ttl_seconds = ttl_seconds
        self.max_tracks = max_tracks
        self.max_sessions_per_user = max_sessions_per_user

    def create(
        self,
        owner_user_id: int,
        chat_id: int,
        preview: PlaylistPreview,
        *,
        now: int | None = None,
    ) -> PlaylistSession:
        if not preview.tracks:
            raise PlaylistSessionError("playlist is empty")
        if len(preview.tracks) > self.max_tracks:
            raise PlaylistTooLarge(
                f"В плейлисте {len(preview.tracks)} треков; максимум для бота — "
                f"{self.max_tracks}."
            )

        current = int(time.time()) if now is None else now
        expires_at = current + self.ttl_seconds
        token = secrets.token_urlsafe(16)
        token_hash = self._token_hash(token)
        try:
            with self._connection() as db:
                self._begin(db)
                self._delete_expired(db, current)
                rows = db.execute(
                    """
                    SELECT token_hash FROM playlist_sessions
                    WHERE owner_user_id = ?
                    ORDER BY created_at ASC, token_hash ASC
                    """,
                    (owner_user_id,),
                ).fetchall()
                overflow = len(rows) - self.max_sessions_per_user + 1
                for row in rows[: max(0, overflow)]:
                    db.execute(
                        "DELETE FROM playlist_sessions WHERE token_hash = ?",
                        (str(row[0]),),
                    )

                db.execute(
                    """
                    INSERT INTO playlist_sessions(
                        token_hash, owner_user_id, chat_id, source_url, name,
                        track_count, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        owner_user_id,
                        chat_id,
                        preview.source_url,
                        preview.name,
                        len(preview.tracks),
                        current,
                        expires_at,
                    ),
                )
                db.executemany(
                    """
                    INSERT INTO playlist_tracks(token_hash, ordinal, metadata_json)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (token_hash, index, self._encode_track(track))
                        for index, track in enumerate(preview.tracks)
                    ],
                )
                db.execute("COMMIT")
            self._protect_file()
        except PlaylistSessionError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            logger.exception("Could not create playlist session")
            self._protect_file()
            raise PlaylistSessionError("playlist database is unavailable") from exc

        return PlaylistSession(
            token=token,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=None,
            source_url=preview.source_url,
            name=preview.name,
            tracks=preview.tracks,
            expires_at=expires_at,
        )

    def bind_message(
        self,
        token: str,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> bool:
        token_hash = self._token_hash(token)
        try:
            with self._connection() as db:
                self._begin(db)
                updated = db.execute(
                    """
                    UPDATE playlist_sessions SET message_id = ?
                    WHERE token_hash = ? AND owner_user_id = ? AND chat_id = ?
                      AND message_id IS NULL
                    """,
                    (message_id, token_hash, owner_user_id, chat_id),
                ).rowcount
                db.execute("COMMIT")
            self._protect_file()
            return updated == 1
        except (OSError, sqlite3.Error) as exc:
            self._protect_file()
            raise PlaylistSessionError("playlist database is unavailable") from exc

    def get(
        self,
        token: str,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        *,
        now: int | None = None,
    ) -> PlaylistSession | None:
        current = int(time.time()) if now is None else now
        token_hash = self._token_hash(token)
        try:
            with self._connection() as db:
                self._begin(db)
                self._delete_expired(db, current)
                row = db.execute(
                    """
                    SELECT message_id, source_url, name, track_count, expires_at
                    FROM playlist_sessions
                    WHERE token_hash = ? AND owner_user_id = ? AND chat_id = ?
                      AND message_id = ? AND expires_at >= ?
                    """,
                    (token_hash, owner_user_id, chat_id, message_id, current),
                ).fetchone()
                if row is None:
                    db.execute("COMMIT")
                    self._protect_file()
                    return None
                track_rows = db.execute(
                    """
                    SELECT metadata_json FROM playlist_tracks
                    WHERE token_hash = ? ORDER BY ordinal ASC
                    """,
                    (token_hash,),
                ).fetchall()
                db.execute("COMMIT")
            self._protect_file()
            if len(track_rows) != int(row[3]):
                raise PlaylistSessionError("playlist session is incomplete")
            tracks = tuple(self._decode_track(str(track_row[0])) for track_row in track_rows)
            return PlaylistSession(
                token=token,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                message_id=int(row[0]),
                source_url=str(row[1]),
                name=str(row[2]),
                tracks=tracks,
                expires_at=int(row[4]),
            )
        except PlaylistSessionError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.exception("Could not read playlist session")
            self._protect_file()
            raise PlaylistSessionError("playlist database is unavailable") from exc

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256((token or "").encode("ascii", errors="ignore")).hexdigest()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute(CREATE_SESSIONS_TABLE)
        db.execute(CREATE_TRACKS_TABLE)

    def _begin(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_schema(db)
        except Exception:
            db.execute("ROLLBACK")
            raise

    @staticmethod
    def _delete_expired(db: sqlite3.Connection, now: int) -> None:
        db.execute("DELETE FROM playlist_sessions WHERE expires_at < ?", (now,))

    @staticmethod
    def _encode_track(track: TrackMetadata) -> str:
        return json.dumps(asdict(track), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_track(payload: str) -> TrackMetadata:
        data = json.loads(payload)
        if not isinstance(data, dict) or not set(data).issubset(_TRACK_FIELDS):
            raise PlaylistSessionError("playlist metadata is invalid")
        return TrackMetadata(**data)

    def _protect_file(self) -> None:
        if not self.path.exists():
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.warning("Could not restrict playlist database permissions: %s", self.path)
