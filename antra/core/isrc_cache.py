"""
Persistent Spotify track ID → ISRC cache (v1.1.8 FEAT-6).

A persistent `trackID → ISRC` store means a track resolved once is never looked
up again. Antra had no persistent ISRC cache at all: every run
re-resolved the same ISRCs from scratch, which costs API calls and is a genuine
rate-limit exposure — v1.1.3 BUG-12 was exactly this, ISRC enrichment tripping
Spotify's 429 and bailing with 0/12 tracks enriched.

Design decisions:

  * **SQLite, not a second embedded key-value store.** `provider_stats.py`
    already establishes the pattern in this codebase (SQLite in the platformdirs
    user-data dir, every operation degrading to a no-op on failure). Adding a
    new dependency for one small table would not be worth it.
  * **Resolution source is stored.** ISRC accuracy became a correctness concern in
    v1.1.8 BUG-1, where adapters were fabricating confidence. Recording *how* an
    ISRC was resolved means a future session can invalidate one source's entries
    without discarding the whole cache.
  * **Entries expire.** A permanent cache would be simpler. A wrong ISRC — from a
    mis-tagged release or a source that later corrects itself — would be cached
    forever and would keep producing the same wrong match on every run, which is
    precisely the failure mode BUG-1 was about. A TTL bounds that damage.

Negative results are cached separately and briefly: "this track has no ISRC" is
worth remembering for one session so a playlist does not re-query it per run, but
not for weeks, because catalogues do get corrected.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# A cached ISRC older than this is re-resolved. Long enough to save real work
# across sessions, short enough that a wrong entry cannot persist indefinitely.
ISRC_TTL_SECONDS = 30 * 24 * 3600  # 30 days
# "No ISRC found" is much cheaper to be wrong about in the other direction, so it
# expires quickly — a catalogue that gains an ISRC should be picked up soon.
NEGATIVE_TTL_SECONDS = 24 * 3600

_INSTANCES: dict[str, "ISRCCache"] = {}
_INSTANCES_LOCK = threading.Lock()


def get_isrc_cache(db_path: Optional[str] = None) -> Optional["ISRCCache"]:
    """Return a process-wide ISRCCache for db_path (cached), or None on failure."""
    path = db_path or ISRCCache.default_path()
    if not path:
        return None
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            try:
                inst = ISRCCache(Path(path))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[ISRCCache] disabled (could not open {path}): {e}")
                return None
            _INSTANCES[path] = inst
        return inst


class ISRCCache:
    """Spotify track ID → ISRC memory backed by SQLite."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS track_isrc (
        track_id   TEXT PRIMARY KEY,
        isrc       TEXT,
        source     TEXT,
        updated_at INTEGER DEFAULT 0
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
        from antra.utils.runtime import get_app_data_dir
        return str(get_app_data_dir() / "isrc_cache.db")

    # ── reads ──────────────────────────────────────────────────────────────
    def get(self, track_id: str) -> Optional[str]:
        """Return a cached ISRC, "" for a cached negative, or None if unknown.

        The three-way return matters: "" lets the caller skip a lookup it already
        knows will fail, while None means "never looked".
        """
        tid = (track_id or "").strip()
        if not tid:
            return None
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT isrc, updated_at FROM track_isrc WHERE track_id = ?", (tid,)
                ).fetchone()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ISRCCache] get({tid}) failed: {e}")
            return None
        if not row:
            return None
        isrc, updated = (row[0] or ""), int(row[1] or 0)
        age = time.time() - updated
        ttl = ISRC_TTL_SECONDS if isrc else NEGATIVE_TTL_SECONDS
        if age > ttl:
            return None
        return isrc

    def get_many(self, track_ids: list[str], include_negative: bool = False) -> dict[str, str]:
        """Bulk lookup — one query for a whole playlist rather than N.

        Fresh cached negatives ("Spotify has no ISRC") are returned as "" only
        when include_negative is set; they used to be dropped unconditionally,
        which made the negative cache write-only.
        """
        ids = [t.strip() for t in (track_ids or []) if t and t.strip()]
        if not ids:
            return {}
        out: dict[str, str] = {}
        try:
            with self._lock:
                marks = ",".join("?" * len(ids))
                rows = self._db.execute(
                    f"SELECT track_id, isrc, updated_at FROM track_isrc WHERE track_id IN ({marks})",
                    ids,
                ).fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ISRCCache] get_many failed: {e}")
            return {}
        now = time.time()
        for tid, isrc, updated in rows:
            isrc = isrc or ""
            ttl = ISRC_TTL_SECONDS if isrc else NEGATIVE_TTL_SECONDS
            if now - int(updated or 0) <= ttl and (isrc or include_negative):
                out[tid] = isrc
        return out

    # ── writes ─────────────────────────────────────────────────────────────
    def put(self, track_id: str, isrc: Optional[str], source: str = "") -> None:
        """Cache an ISRC (or a negative result when isrc is falsy)."""
        tid = (track_id or "").strip()
        if not tid:
            return
        value = (isrc or "").strip().upper()
        try:
            with self._lock:
                self._db.execute(
                    """
                    INSERT INTO track_isrc (track_id, isrc, source, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                        isrc = excluded.isrc,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (tid, value, (source or "").strip(), int(time.time())),
                )
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ISRCCache] put({tid}) failed: {e}")

    def put_many(self, pairs: dict[str, str], source: str = "") -> None:
        """Cache a batch of resolutions in one transaction."""
        if not pairs:
            return
        now = int(time.time())
        rows = [
            (tid.strip(), (isrc or "").strip().upper(), (source or "").strip(), now)
            for tid, isrc in pairs.items()
            if tid and tid.strip()
        ]
        if not rows:
            return
        try:
            with self._lock:
                self._db.executemany(
                    """
                    INSERT INTO track_isrc (track_id, isrc, source, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                        isrc = excluded.isrc,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[ISRCCache] put_many failed: {e}")

    def stats(self) -> dict:
        """Small diagnostic summary (used by tests and logging)."""
        try:
            with self._lock:
                total = self._db.execute("SELECT COUNT(*) FROM track_isrc").fetchone()[0]
                positive = self._db.execute(
                    "SELECT COUNT(*) FROM track_isrc WHERE isrc != ''"
                ).fetchone()[0]
        except Exception:  # pragma: no cover - defensive
            return {"total": 0, "positive": 0, "negative": 0}
        return {"total": total, "positive": positive, "negative": total - positive}
