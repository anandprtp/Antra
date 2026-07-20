import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WebSessionStoreError(RuntimeError):
    """Raised when the web session database cannot be read or updated safely."""


class PlayerStateConflict(RuntimeError):
    """Raised when a stale player-state revision is submitted."""

    def __init__(self, current: "PlayerState"):
        super().__init__("player state revision is stale")
        self.current = current


@dataclass(frozen=True)
class WebIdentity:
    user_id: int
    role: str
    expires_at: int


@dataclass(frozen=True)
class PlayerLaunch:
    user_id: int
    role: str
    media_id: str | None


@dataclass(frozen=True)
class PlayerState:
    queue_ids: tuple[str, ...] = ()
    current_id: str | None = None
    position_ms: int = 0
    paused: bool = True
    shuffle: bool = False
    repeat_mode: str = "off"
    revision: int = 0
    updated_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_ids": list(self.queue_ids),
            "current_id": self.current_id,
            "position_ms": self.position_ms,
            "paused": self.paused,
            "shuffle": self.shuffle,
            "repeat_mode": self.repeat_mode,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


def validate_player_state(
    value: Any,
    *,
    max_queue_items: int = 500,
) -> PlayerState:
    if not isinstance(value, dict):
        raise ValueError("player state must be an object")

    queue_ids = value.get("queue_ids", [])
    if not isinstance(queue_ids, list) or len(queue_ids) > max_queue_items:
        raise ValueError("queue_ids must be a bounded array")
    if any(
        not isinstance(media_id, str)
        or not media_id
        or len(media_id) > 128
        for media_id in queue_ids
    ):
        raise ValueError("queue_ids contains an invalid media ID")

    current_id = value.get("current_id")
    if current_id is not None and (
        not isinstance(current_id, str)
        or not current_id
        or len(current_id) > 128
    ):
        raise ValueError("current_id is invalid")
    if current_id is not None and current_id not in queue_ids:
        raise ValueError("current_id must be present in queue_ids")

    position_ms = value.get("position_ms", 0)
    if (
        isinstance(position_ms, bool)
        or not isinstance(position_ms, int)
        or position_ms < 0
        or position_ms > 31 * 24 * 60 * 60 * 1000
    ):
        raise ValueError("position_ms is invalid")

    paused = value.get("paused", True)
    shuffle = value.get("shuffle", False)
    if not isinstance(paused, bool) or not isinstance(shuffle, bool):
        raise ValueError("paused and shuffle must be booleans")

    repeat_mode = value.get("repeat_mode", "off")
    if repeat_mode not in {"off", "all", "one"}:
        raise ValueError("repeat_mode is invalid")

    revision = value.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision is invalid")

    return PlayerState(
        queue_ids=tuple(queue_ids),
        current_id=current_id,
        position_ms=position_ms,
        paused=paused,
        shuffle=shuffle,
        repeat_mode=repeat_mode,
        revision=revision,
    )


class WebSessionStore:
    """Persistent bearer sessions and per-user browser player state."""

    def __init__(
        self,
        path: Path,
        *,
        default_ttl_seconds: int = 30 * 24 * 60 * 60,
        max_queue_items: int = 500,
    ):
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        if max_queue_items <= 0:
            raise ValueError("max_queue_items must be positive")
        self.path = path.expanduser().resolve()
        self.default_ttl_seconds = default_ttl_seconds
        self.max_queue_items = max_queue_items
        self._initialize()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS web_sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        revoked_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS web_sessions_user_id
                        ON web_sessions(user_id);

                    CREATE TABLE IF NOT EXISTS player_launches (
                        token_hash TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        media_id TEXT,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        consumed_at INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS player_states (
                        user_id INTEGER PRIMARY KEY,
                        queue_json TEXT NOT NULL,
                        current_id TEXT,
                        position_ms INTEGER NOT NULL,
                        paused INTEGER NOT NULL,
                        shuffle INTEGER NOT NULL,
                        repeat_mode TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    """
                )
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise WebSessionStoreError("failed to initialize web session store") from exc

    def issue(
        self,
        user_id: int,
        *,
        role: str = "member",
        ttl_seconds: int | None = None,
        now: int | None = None,
    ) -> str:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if role not in {"admin", "member"}:
            raise ValueError("role must be admin or member")
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(32)
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM web_sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                    (current,),
                )
                connection.execute(
                    """
                    INSERT INTO web_sessions
                        (token_hash, user_id, role, created_at, expires_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        self._token_hash(token),
                        user_id,
                        role,
                        current,
                        current + ttl,
                    ),
                )
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to issue web session") from exc
        return token

    def authenticate(self, token: str, *, now: int | None = None) -> WebIdentity | None:
        if not isinstance(token, str) or not token:
            return None
        current = int(time.time()) if now is None else now
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT user_id, role, expires_at
                    FROM web_sessions
                    WHERE token_hash = ? AND revoked_at IS NULL AND expires_at >= ?
                    """,
                    (self._token_hash(token), current),
                ).fetchone()
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to authenticate web session") from exc
        if row is None:
            return None
        role = str(row["role"])
        if role not in {"admin", "member"}:
            raise WebSessionStoreError("stored web session role is invalid")
        return WebIdentity(
            user_id=int(row["user_id"]),
            role=role,
            expires_at=int(row["expires_at"]),
        )

    def revoke(self, token: str, *, now: int | None = None) -> bool:
        if not isinstance(token, str) or not token:
            return False
        current = int(time.time()) if now is None else now
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE web_sessions
                    SET revoked_at = ?
                    WHERE token_hash = ? AND revoked_at IS NULL
                    """,
                    (current, self._token_hash(token)),
                )
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to revoke web session") from exc
        return cursor.rowcount > 0

    def revoke_user(self, user_id: int, *, now: int | None = None) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        current = int(time.time()) if now is None else now
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE web_sessions
                    SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (current, user_id),
                )
                connection.execute(
                    """
                    UPDATE player_launches
                    SET consumed_at = ?
                    WHERE user_id = ? AND consumed_at IS NULL
                    """,
                    (current, user_id),
                )
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to revoke user sessions") from exc
        return max(0, cursor.rowcount)

    def issue_launch(
        self,
        user_id: int,
        *,
        role: str = "member",
        media_id: str | None = None,
        ttl_seconds: int = 300,
        now: int | None = None,
    ) -> str:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if role not in {"admin", "member"}:
            raise ValueError("role must be admin or member")
        if media_id is not None and (
            not isinstance(media_id, str)
            or not media_id
            or len(media_id) > 128
        ):
            raise ValueError("media_id is invalid")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        current = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(32)
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM player_launches WHERE expires_at < ? OR consumed_at IS NOT NULL",
                    (current,),
                )
                connection.execute(
                    """
                    INSERT INTO player_launches (
                        token_hash, user_id, role, media_id, created_at,
                        expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        self._token_hash(token),
                        user_id,
                        role,
                        media_id,
                        current,
                        current + ttl_seconds,
                    ),
                )
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to issue player launch") from exc
        return token

    def consume_launch(
        self,
        token: str,
        *,
        now: int | None = None,
    ) -> PlayerLaunch | None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        current = int(time.time()) if now is None else now
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM player_launches WHERE expires_at < ? OR consumed_at IS NOT NULL",
                    (current,),
                )
                row = connection.execute(
                    """
                    SELECT user_id, role, media_id
                    FROM player_launches
                    WHERE token_hash = ? AND consumed_at IS NULL AND expires_at >= ?
                    """,
                    (self._token_hash(token), current),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                updated = connection.execute(
                    """
                    UPDATE player_launches
                    SET consumed_at = ?
                    WHERE token_hash = ? AND consumed_at IS NULL AND expires_at >= ?
                    """,
                    (current, self._token_hash(token), current),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return None
                role = str(row["role"])
                if role not in {"admin", "member"}:
                    raise WebSessionStoreError("stored player launch role is invalid")
                launch = PlayerLaunch(
                    user_id=int(row["user_id"]),
                    role=role,
                    media_id=str(row["media_id"]) if row["media_id"] else None,
                )
                connection.commit()
                return launch
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except WebSessionStoreError:
            raise
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to consume player launch") from exc

    def get_player_state(self, user_id: int) -> PlayerState:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM player_states WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to load player state") from exc
        return self._state_from_row(row) if row is not None else PlayerState()

    def save_player_state(
        self,
        user_id: int,
        state: PlayerState,
        *,
        expected_revision: int,
        now: int | None = None,
    ) -> PlayerState:
        if expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        validated = validate_player_state(
            state.to_dict(),
            max_queue_items=self.max_queue_items,
        )
        current_time = int(time.time()) if now is None else now
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM player_states WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                current_state = (
                    self._state_from_row(row) if row is not None else PlayerState()
                )
                if current_state.revision != expected_revision:
                    raise PlayerStateConflict(current_state)

                saved = PlayerState(
                    queue_ids=validated.queue_ids,
                    current_id=validated.current_id,
                    position_ms=validated.position_ms,
                    paused=validated.paused,
                    shuffle=validated.shuffle,
                    repeat_mode=validated.repeat_mode,
                    revision=current_state.revision + 1,
                    updated_at=current_time,
                )
                connection.execute(
                    """
                    INSERT INTO player_states (
                        user_id, queue_json, current_id, position_ms, paused,
                        shuffle, repeat_mode, revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        queue_json = excluded.queue_json,
                        current_id = excluded.current_id,
                        position_ms = excluded.position_ms,
                        paused = excluded.paused,
                        shuffle = excluded.shuffle,
                        repeat_mode = excluded.repeat_mode,
                        revision = excluded.revision,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        json.dumps(saved.queue_ids, ensure_ascii=False),
                        saved.current_id,
                        saved.position_ms,
                        int(saved.paused),
                        int(saved.shuffle),
                        saved.repeat_mode,
                        saved.revision,
                        saved.updated_at,
                    ),
                )
                connection.commit()
                return saved
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except PlayerStateConflict:
            raise
        except sqlite3.Error as exc:
            raise WebSessionStoreError("failed to save player state") from exc

    def _state_from_row(self, row: sqlite3.Row) -> PlayerState:
        try:
            queue_ids = json.loads(row["queue_json"])
            if not isinstance(queue_ids, list):
                raise ValueError("stored queue is not an array")
            if row["paused"] not in {0, 1} or row["shuffle"] not in {0, 1}:
                raise ValueError("stored player flags are invalid")
            stored = PlayerState(
                queue_ids=tuple(queue_ids),
                current_id=row["current_id"],
                position_ms=int(row["position_ms"]),
                paused=bool(row["paused"]),
                shuffle=bool(row["shuffle"]),
                repeat_mode=str(row["repeat_mode"]),
                revision=int(row["revision"]),
                updated_at=int(row["updated_at"]),
            )
            validated = validate_player_state(
                stored.to_dict(),
                max_queue_items=self.max_queue_items,
            )
            return PlayerState(
                queue_ids=validated.queue_ids,
                current_id=validated.current_id,
                position_ms=validated.position_ms,
                paused=validated.paused,
                shuffle=validated.shuffle,
                repeat_mode=validated.repeat_mode,
                revision=validated.revision,
                updated_at=stored.updated_at,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WebSessionStoreError("stored player state is invalid") from exc
