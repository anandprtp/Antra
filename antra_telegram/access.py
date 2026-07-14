import hashlib
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS bot_users (
    telegram_user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    added_at INTEGER NOT NULL
)
"""

CREATE_INVITES_TABLE = """
CREATE TABLE IF NOT EXISTS bot_invites (
    token_hash TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_by INTEGER,
    used_at INTEGER
)
"""


class AccessStoreError(RuntimeError):
    """The access database could not safely authorize or update a user."""


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    role: str | None = None
    claimed_admin: bool = False
    joined_by_invite: bool = False


class AccessStore:
    """Race-safe first-admin claim plus single-use member invitations."""

    def __init__(
        self,
        path: Path,
        *,
        static_allowed_user_ids: frozenset[int] = frozenset(),
        allow_first_claim: bool = False,
    ):
        self.path = path.expanduser().resolve()
        self.static_allowed_user_ids = static_allowed_user_ids
        self.allow_first_claim = allow_first_claim

    def authorize_or_claim(self, user_id: int) -> AccessDecision:
        if user_id in self.static_allowed_user_ids:
            return AccessDecision(True, role="admin")
        if self.static_allowed_user_ids:
            return AccessDecision(False)

        try:
            with self._connection() as db:
                self._begin(db)
                row = db.execute(
                    "SELECT role FROM bot_users WHERE telegram_user_id = ?",
                    (user_id,),
                ).fetchone()
                if row is not None:
                    db.execute("COMMIT")
                    self._protect_file()
                    return AccessDecision(True, role=str(row[0]))

                user_count = int(db.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0])
                if user_count == 0 and self.allow_first_claim:
                    db.execute(
                        "INSERT INTO bot_users(telegram_user_id, role, added_at) VALUES (?, 'admin', ?)",
                        (user_id, int(time.time())),
                    )
                    db.execute("COMMIT")
                    self._protect_file()
                    return AccessDecision(True, role="admin", claimed_admin=True)

                db.execute("COMMIT")
                self._protect_file()
                return AccessDecision(False)
        except (OSError, sqlite3.Error) as exc:
            logger.exception("Access database is unavailable")
            self._protect_file()
            raise AccessStoreError("access database is unavailable") from exc

    def create_invite(
        self,
        admin_user_id: int,
        ttl_seconds: int,
        *,
        now: int | None = None,
    ) -> str:
        current = int(time.time()) if now is None else now
        token = secrets.token_urlsafe(24)
        token_hash = self._token_hash(token)
        try:
            with self._connection() as db:
                self._begin(db)
                if not self._is_admin(db, admin_user_id):
                    db.execute("ROLLBACK")
                    raise PermissionError("only an admin can create invitations")
                db.execute(
                    "DELETE FROM bot_invites WHERE expires_at < ? OR used_at IS NOT NULL",
                    (current,),
                )
                db.execute(
                    "INSERT INTO bot_invites(token_hash, created_by, expires_at) VALUES (?, ?, ?)",
                    (token_hash, admin_user_id, current + ttl_seconds),
                )
                db.execute("COMMIT")
                self._protect_file()
                return token
        except PermissionError:
            raise
        except (OSError, sqlite3.Error) as exc:
            logger.exception("Could not create invitation")
            self._protect_file()
            raise AccessStoreError("access database is unavailable") from exc

    def redeem_invite(
        self,
        user_id: int,
        token: str,
        *,
        now: int | None = None,
    ) -> AccessDecision:
        current = int(time.time()) if now is None else now
        token_hash = self._token_hash(token)
        try:
            with self._connection() as db:
                self._begin(db)
                existing = db.execute(
                    "SELECT role FROM bot_users WHERE telegram_user_id = ?",
                    (user_id,),
                ).fetchone()
                if existing is not None:
                    db.execute("COMMIT")
                    return AccessDecision(True, role=str(existing[0]))

                invite = db.execute(
                    """
                    SELECT expires_at FROM bot_invites
                    WHERE token_hash = ? AND used_at IS NULL
                    """,
                    (token_hash,),
                ).fetchone()
                if invite is None or int(invite[0]) < current:
                    db.execute("COMMIT")
                    return AccessDecision(False)

                db.execute(
                    "INSERT INTO bot_users(telegram_user_id, role, added_at) VALUES (?, 'member', ?)",
                    (user_id, current),
                )
                updated = db.execute(
                    """
                    UPDATE bot_invites SET used_by = ?, used_at = ?
                    WHERE token_hash = ? AND used_at IS NULL
                    """,
                    (user_id, current, token_hash),
                ).rowcount
                if updated != 1:
                    db.execute("ROLLBACK")
                    return AccessDecision(False)
                db.execute("COMMIT")
                self._protect_file()
                return AccessDecision(True, role="member", joined_by_invite=True)
        except (OSError, sqlite3.Error) as exc:
            logger.exception("Could not redeem invitation")
            self._protect_file()
            raise AccessStoreError("access database is unavailable") from exc

    def list_members(self, admin_user_id: int) -> list[tuple[int, str]]:
        if self.static_allowed_user_ids:
            if admin_user_id not in self.static_allowed_user_ids:
                raise PermissionError("only an admin can list members")
            return sorted((user_id, "admin") for user_id in self.static_allowed_user_ids)
        try:
            with self._connection() as db:
                self._begin(db)
                if not self._is_admin(db, admin_user_id):
                    db.execute("ROLLBACK")
                    raise PermissionError("only an admin can list members")
                rows = db.execute(
                    "SELECT telegram_user_id, role FROM bot_users ORDER BY role, telegram_user_id"
                ).fetchall()
                db.execute("COMMIT")
                return [(int(row[0]), str(row[1])) for row in rows]
        except PermissionError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise AccessStoreError("access database is unavailable") from exc

    def admin_id(self) -> int | None:
        if self.static_allowed_user_ids:
            return min(self.static_allowed_user_ids)
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path, timeout=5) as db:
                row = db.execute(
                    "SELECT telegram_user_id FROM bot_users WHERE role = 'admin' LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise AccessStoreError("access database is unavailable") from exc
        return int(row[0]) if row else None

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute(CREATE_USERS_TABLE)
        db.execute(CREATE_INVITES_TABLE)

    def _begin(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_schema(db)
        except Exception:
            db.execute("ROLLBACK")
            raise

    def _is_admin(self, db: sqlite3.Connection, user_id: int) -> bool:
        if user_id in self.static_allowed_user_ids:
            return True
        row = db.execute(
            "SELECT role FROM bot_users WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
        return row is not None and str(row[0]) == "admin"

    def _protect_file(self) -> None:
        if not self.path.exists():
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.warning("Could not restrict access database permissions: %s", self.path)
