from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


SAFE_ROOT_FILES = {
    ".cache",
    ".spotipyoauthcache",
    "antra.log",
}

SAFE_ROOT_DIRS = {
    "build",
    "dist",
}

SAFE_CACHE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

SAFE_TEMP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".tmp",
    ".temp",
    ".part",
    ".aria2",
    ".ytdl",
}

SKIP_DIR_NAMES = {
    ".git",
    "Music",
    "Albums",
    "Playlists",
    "venv",
}


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    kind: str
    size_bytes: int


@dataclass(frozen=True)
class CleanupReport:
    candidates: list[CleanupCandidate]
    files_removed: int
    dirs_removed: int
    bytes_reclaimed: int


def _measure_path(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _candidate(path: Path, kind: str) -> CleanupCandidate:
    return CleanupCandidate(path=path, kind=kind, size_bytes=_measure_path(path))


def find_cleanup_candidates(repo_root: str | Path) -> list[CleanupCandidate]:
    root = Path(repo_root).resolve()
    candidates: dict[Path, CleanupCandidate] = {}

    for name in SAFE_ROOT_FILES:
        path = root / name
        if path.is_file():
            candidates[path] = _candidate(path, "file")

    for name in SAFE_ROOT_DIRS:
        path = root / name
        if path.is_dir():
            candidates[path] = _candidate(path, "dir")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SAFE_ROOT_DIRS and name not in SKIP_DIR_NAMES
        ]

        for dirname in list(dirnames):
            if dirname in SAFE_CACHE_DIRS:
                path = current / dirname
                candidates[path] = _candidate(path, "dir")
                dirnames.remove(dirname)

        for filename in filenames:
            path = current / filename
            if path in candidates:
                continue
            if path.suffix.lower() in SAFE_TEMP_SUFFIXES:
                candidates[path] = _candidate(path, "file")

    return sorted(candidates.values(), key=lambda item: str(item.path).lower())


def cleanup_project_junk(repo_root: str | Path) -> CleanupReport:
    candidates = find_cleanup_candidates(repo_root)
    files_removed = 0
    dirs_removed = 0
    bytes_reclaimed = 0

    for candidate in candidates:
        if not candidate.path.exists():
            continue
        bytes_reclaimed += candidate.size_bytes
        if candidate.kind == "dir":
            shutil.rmtree(candidate.path)
            dirs_removed += 1
        else:
            candidate.path.unlink()
            files_removed += 1

    return CleanupReport(
        candidates=candidates,
        files_removed=files_removed,
        dirs_removed=dirs_removed,
        bytes_reclaimed=bytes_reclaimed,
    )
