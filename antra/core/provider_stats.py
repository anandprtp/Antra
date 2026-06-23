"""
Persistent provider (adapter) reliability memory.

SpotiFLAC keeps a bbolt-backed record of which download providers recently
succeeded or failed and reorders them on the next request. Antra's resolver only
had in-memory cooldowns that reset on every app restart — a Tidal mirror that
served preview clips all session would still be tried first after a relaunch.

ProviderStats persists per-adapter success/failure outcomes to a small SQLite
file in the user data directory. The resolver consults it to reorder adapters
*within each quality tier* (never across tiers — quality priority always wins):
recently-proven adapters are tried first, recently-broken ones last, everyone
else keeps their normal (rotated / priority) order.

Robustness rules:
  - The latest outcome decides the bucket, so a transient failure self-heals the
    moment the adapter succeeds again.
  - A failure older than FAILURE_TTL_SECONDS decays back to neutral, so one bad
    night does not bury an otherwise-healthy adapter forever.
  - Every operation degrades gracefully — if the DB cannot be opened or a query
    fails, ranking becomes a no-op and downloads proceed exactly as before.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# A failure older than this is treated as neutral rather than burying the adapter
# forever. Mirrors the resolver's "cooldowns are temporary" stance.
FAILURE_TTL_SECONDS = 6 * 3600

# Bucket values used for ranking (higher = tried earlier).
_BUCKET_SUCCESS = 2
_BUCKET_NEUTRAL = 1
_BUCKET_FAILURE = 0

_INSTANCES: dict[str, "ProviderStats"] = {}
_INSTANCES_LOCK = threading.Lock()


def get_provider_stats(db_path: Optional[str] = None) -> Optional["ProviderStats"]:
    """Return a process-wide ProviderStats for db_path (cached), or None on failure.

    The instance is cached per path so build_engine() — called once per download —
    reuses a single SQLite connection instead of opening a new one each time.
    """
    path = db_path or ProviderStats.default_path()
    if not path:
        return None
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            try:
                inst = ProviderStats(Path(path))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[ProviderStats] disabled (could not open {path}): {e}")
                return None
            _INSTANCES[path] = inst
        return inst


class ProviderStats:
    """Per-adapter success/failure memory backed by SQLite."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS provider_outcomes (
        adapter        TEXT PRIMARY KEY,
        last_outcome   TEXT,
        last_success   INTEGER DEFAULT 0,
        last_failure   INTEGER DEFAULT 0,
        success_count  INTEGER DEFAULT 0,
        failure_count  INTEGER DEFAULT 0
    )"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(self._SCHEMA)
            self._db.commit()

    @staticmethod
    def default_path() -> str:
        try:
            from platformdirs import user_data_dir
            base = user_data_dir("Antra", "Antra")
        except Exception:
            base = str(Path(__file__).resolve().parents[2])
        return str(Path(base) / "provider_stats.db")

    def record(self, adapter: str, success: bool) -> None:
        """Record one download outcome for an adapter. Failures are non-fatal."""
        if not adapter:
            return
        now = int(time.time())
        try:
            with self._lock:
                self._db.execute(
                    """
                    INSERT INTO provider_outcomes
                        (adapter, last_outcome, last_success, last_failure, success_count, failure_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(adapter) DO UPDATE SET
                        last_outcome  = excluded.last_outcome,
                        last_success  = CASE WHEN excluded.last_outcome='success'
                                             THEN excluded.last_success ELSE last_success END,
                        last_failure  = CASE WHEN excluded.last_outcome='failure'
                                             THEN excluded.last_failure ELSE last_failure END,
                        success_count = success_count + excluded.success_count,
                        failure_count = failure_count + excluded.failure_count
                    """,
                    (
                        adapter,
                        "success" if success else "failure",
                        now if success else 0,
                        0 if success else now,
                        1 if success else 0,
                        0 if success else 1,
                    ),
                )
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ProviderStats] record({adapter}) failed: {e}")

    def _buckets(self) -> dict[str, int]:
        """adapter name → bucket (success / neutral / recent-failure)."""
        out: dict[str, int] = {}
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT adapter, last_outcome, last_failure FROM provider_outcomes"
                ).fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ProviderStats] read failed: {e}")
            return out
        now = time.time()
        for adapter, last_outcome, last_failure in rows:
            if last_outcome == "success":
                out[adapter] = _BUCKET_SUCCESS
            elif last_outcome == "failure" and (now - (last_failure or 0)) < FAILURE_TTL_SECONDS:
                out[adapter] = _BUCKET_FAILURE
            else:
                out[adapter] = _BUCKET_NEUTRAL
        return out

    def rank(self, adapters: list) -> list:
        """Stable-sort adapters: recent-success first, recent-failure last.

        Equal-bucket adapters keep their incoming order, so the resolver's
        per-tier rotation / priority order is preserved for load distribution.
        """
        if len(adapters) <= 1:
            return adapters
        buckets = self._buckets()
        if not buckets:
            return adapters
        # sorted() is stable even with reverse=True — equal keys retain input order.
        return sorted(
            adapters,
            key=lambda a: buckets.get(getattr(a, "name", ""), _BUCKET_NEUTRAL),
            reverse=True,
        )
