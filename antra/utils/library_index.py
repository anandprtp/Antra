"""
Persistent library identity index (v2.0.0 SF-4).

`LibraryOrganizer._build_identity_index()` walked the entire library on **every
run** and re-read the tags of every audio file to rebuild its dedup index. That
is pure repeated work: the previous walk found the same files with the same
tags. Measured on a synthetic 2,000-file library, the walk costs ~8.1 s cold and
~1.2 s warm, and it is paid before a single byte is downloaded.

This module caches the per-file identity keys in SQLite, keyed on
(root, path, mtime_ns, size). A file whose stat has not changed is served from
the cache and never re-parsed by mutagen.

Design rules, each of which is load-bearing:

  - **The cache is keyed by the key-derivation LOGIC, not just the file.** If
    `_identity_keys_from_values` (or any of its helpers) changes, every cached
    row is stale in a way that is invisible — a wrong dedup decision does not
    look like a cache bug, it looks like a duplicate file or a silently skipped
    track. The version is therefore derived automatically from the *source* of
    the key-producing functions, with a manual constant as the outer namespace
    so the cache can also be invalidated by hand.

  - **Never a behaviour change.** A hit must reproduce exactly what reading the
    file would have produced, including the resolved path string. Anything the
    cache cannot answer falls through to the real read.

  - **Every operation degrades to a no-op.** If the DB cannot be opened, a query
    fails, or the stored payload is malformed, the caller re-reads the files and
    behaves exactly as it did before this module existed. A cache must never be
    able to break a download.

Follows the established pattern of `provider_stats.py` / `isrc_cache.py`:
SQLite in the platformdirs user-data directory, one process-wide instance per
path, every failure swallowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Outer cache namespace. Bump this by hand to invalidate every cached row — the
# escape hatch for the case where the automatic source fingerprint below cannot
# be computed (e.g. a frozen build with no source available).
MANUAL_INDEX_VERSION = 1

_INSTANCES: dict[str, "LibraryIndex"] = {}
_INSTANCES_LOCK = threading.Lock()


def get_library_index(db_path: Optional[str] = None) -> Optional["LibraryIndex"]:
    """Return a process-wide LibraryIndex for db_path (cached), or None.

    Cached per path so repeated LibraryOrganizer construction in one process
    reuses a single SQLite connection.
    """
    path = db_path or LibraryIndex.default_path()
    if not path:
        return None
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            try:
                inst = LibraryIndex(Path(path))
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"[LibraryIndex] disabled (could not open {path}): {e}")
                return None
            _INSTANCES[path] = inst
        return inst


def compute_logic_version(functions: Iterable[object]) -> str:
    """Fingerprint the key-derivation logic so a code change invalidates the cache.

    Hashing the *source* of the functions that produce identity keys means an
    edit to any of them automatically orphans every cached row, rather than
    relying on a human remembering to bump a constant. If source is unavailable
    (a frozen build without the .py files), the fingerprint degrades to
    "nosrc" and only the manual version applies — which is exactly why the
    manual version exists.
    """
    import inspect

    h = hashlib.sha256()
    ok = False
    for fn in functions:
        target = getattr(fn, "__func__", fn)
        try:
            src = inspect.getsource(target)
        except Exception:
            continue
        ok = True
        h.update(src.encode("utf-8", errors="replace"))
    suffix = h.hexdigest()[:16] if ok else "nosrc"
    return f"{MANUAL_INDEX_VERSION}:{suffix}"


class LibraryIndex:
    """Per-file identity-key cache backed by SQLite."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS library_files (
        root      TEXT NOT NULL,
        path      TEXT NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        size      INTEGER NOT NULL,
        version   TEXT NOT NULL,
        resolved  TEXT NOT NULL,
        keys      TEXT NOT NULL,
        PRIMARY KEY (root, path)
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
        return str(get_app_data_dir() / "library_index.db")

    # ── Reads ─────────────────────────────────────────────────────────────

    def load_root(self, root: str, version: str) -> dict[str, tuple[int, int, str, list[str]]]:
        """Return {path: (mtime_ns, size, resolved, keys)} for one root.

        Only rows written by the same logic version are returned — a version
        mismatch is treated as a total miss, so stale keys can never be served.
        One query for the whole root rather than one per file: a 20,000-file
        library is one round trip, not 20,000.
        """
        out: dict[str, tuple[int, int, str, list[str]]] = {}
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT path, mtime_ns, size, resolved, keys FROM library_files "
                    "WHERE root = ? AND version = ?",
                    (root, version),
                ).fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LibraryIndex] read failed for {root}: {e}")
            return out
        for path, mtime_ns, size, resolved, keys_json in rows:
            try:
                keys = json.loads(keys_json)
            except Exception:
                continue  # malformed payload — treat as a miss, re-read the file
            if not isinstance(keys, list):
                continue
            out[path] = (mtime_ns, size, resolved, [str(k) for k in keys])
        return out

    # ── Writes ────────────────────────────────────────────────────────────

    def store(
        self,
        root: str,
        version: str,
        entries: Iterable[tuple[str, int, int, str, list[str]]],
    ) -> None:
        """Upsert (path, mtime_ns, size, resolved, keys) rows for one root."""
        rows = [
            (root, path, int(mtime_ns), int(size), version, resolved, json.dumps(keys))
            for path, mtime_ns, size, resolved, keys in entries
        ]
        if not rows:
            return
        try:
            with self._lock:
                self._db.executemany(
                    "INSERT INTO library_files (root, path, mtime_ns, size, version, resolved, keys) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(root, path) DO UPDATE SET "
                    "  mtime_ns = excluded.mtime_ns, size = excluded.size, "
                    "  version  = excluded.version,  resolved = excluded.resolved, "
                    "  keys     = excluded.keys",
                    rows,
                )
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LibraryIndex] store failed for {root}: {e}")

    def prune_missing_roots(self) -> int:
        """Drop every row whose library root no longer exists on disk.

        Without this the table only ever grows: a user who changes their music
        folder, or a test that builds a throwaway library, leaves rows nothing
        will ever scan again (per-root prune only runs for roots we still open).
        Cheap — one query plus one `exists()` per distinct root.
        """
        try:
            with self._lock:
                roots = [r[0] for r in self._db.execute(
                    "SELECT DISTINCT root FROM library_files"
                ).fetchall()]
            gone = [r for r in roots if not Path(r).exists()]
            if not gone:
                return 0
            with self._lock:
                self._db.executemany(
                    "DELETE FROM library_files WHERE root = ?", [(r,) for r in gone]
                )
                self._db.commit()
            return len(gone)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LibraryIndex] prune_missing_roots failed: {e}")
            return 0

    def prune(self, root: str, seen_paths: set[str]) -> int:
        """Delete rows for this root whose file no longer exists.

        Scoped to one root so a second library is never touched. Deleting by
        difference (rather than "delete all, re-insert") keeps the common
        no-change case at zero writes.
        """
        try:
            with self._lock:
                existing = {
                    r[0]
                    for r in self._db.execute(
                        "SELECT path FROM library_files WHERE root = ?", (root,)
                    ).fetchall()
                }
                gone = existing - seen_paths
                if not gone:
                    return 0
                self._db.executemany(
                    "DELETE FROM library_files WHERE root = ? AND path = ?",
                    [(root, p) for p in gone],
                )
                self._db.commit()
                return len(gone)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[LibraryIndex] prune failed for {root}: {e}")
            return 0
