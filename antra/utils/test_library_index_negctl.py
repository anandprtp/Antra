"""
Negative control for the SF-4 library index (v2.0.0).

Reverts each load-bearing property in place, asserts `test_library_index.py`
goes RED, then restores the file byte-for-byte. A green suite proves nothing
until it has been shown to fail against broken code.

Run:  python antra/utils/test_library_index_negctl.py
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER = ROOT / "antra" / "utils" / "organizer.py"
INDEX = ROOT / "antra" / "utils" / "library_index.py"
SUITE = ROOT / "antra" / "utils" / "test_library_index.py"


def read(p: Path) -> str:
    return io.open(p, encoding="utf-8", newline="").read()


def write(p: Path, s: str) -> None:
    io.open(p, "w", encoding="utf-8", newline="").write(s)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def suite_passes() -> bool:
    r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode == 0


# The prune scoping, as one block: the SELECT and the DELETE are scoped
# independently and BOTH must go for the hazard to appear.
_PRUNE_SCOPED = (
    '                        "SELECT path FROM library_files WHERE root = ?", (root,)\n'
    "                    ).fetchall()\n"
    "                }\n"
    "                gone = existing - seen_paths\n"
    "                if not gone:\n"
    "                    return 0\n"
    "                self._db.executemany(\n"
    '                    "DELETE FROM library_files WHERE root = ? AND path = ?",\n'
    "                    [(root, p) for p in gone],\n"
)

_PRUNE_UNSCOPED = (
    '                        "SELECT path FROM library_files", ()\n'
    "                    ).fetchall()\n"
    "                }\n"
    "                gone = existing - seen_paths\n"
    "                if not gone:\n"
    "                    return 0\n"
    "                self._db.executemany(\n"
    '                    "DELETE FROM library_files WHERE path = ?",\n'
    "                    [(p,) for p in gone],\n"
)


# (label, file, old, new) — `old` must appear exactly once.
SABOTAGES = [
    (
        "cache ignores mtime/size (serves a stale row for a changed file)",
        ORGANIZER,
        "if row is not None and row[0] == stat.st_mtime_ns and row[1] == stat.st_size:",
        "if row is not None:",
    ),
    (
        "cache ignores the logic version (serves keys from other code)",
        INDEX,
        '"WHERE root = ? AND version = ?",\n                    (root, version),',
        '"WHERE root = ?",\n                    (root,),',
    ),
    (
        "prune is not scoped by root (scanning one library wipes another)",
        INDEX,
        _PRUNE_SCOPED,
        _PRUNE_UNSCOPED,
    ),
    (
        "deleted files are never pruned",
        ORGANIZER,
        "            index.prune(self._index_root, seen)",
        "            pass  # prune disabled",
    ),
    (
        "a malformed payload yields empty keys instead of a re-read",
        INDEX,
        "            except Exception:\n                continue  # malformed payload — treat as a miss, re-read the file",
        "            except Exception:\n                keys = []",
    ),
    (
        "full_albums mode builds the index anyway",
        ORGANIZER,
        "        if library_index_enabled and not full_albums:",
        "        if library_index_enabled:",
    ),
    (
        "prune_missing_roots deletes every root, not just vanished ones",
        INDEX,
        "            gone = [r for r in roots if not Path(r).exists()]",
        "            gone = list(roots)",
    ),
    (
        "the logic fingerprint is constant (source edits stop invalidating)",
        INDEX,
        'suffix = h.hexdigest()[:16] if ok else "nosrc"',
        'suffix = "constant"',
    ),
    (
        "the env kill switch is ignored",
        ROOT / "antra" / "core" / "config.py",
        'library_index_enabled=os.getenv("LIBRARY_INDEX_ENABLED", "true").lower() == "true",',
        "library_index_enabled=True,",
    ),
    (
        "json_cli stops passing the setting through",
        ROOT / "antra" / "json_cli.py",
        '        library_index_enabled=getattr(cfg, "library_index_enabled", True),\n',
        "",
    ),
]


# Deliberately NOT sabotaged — unobservable by construction, recorded rather
# than claimed. Replacing the cached `resolved` value with `str(file_path)`
# changes nothing, because `LibraryOrganizer.__init__` does
# `self.root = Path(root).resolve()`, so every path `rglob` yields is already
# canonical and `str(p) == str(p.resolve())` for all of them. The suite pins
# that invariant directly instead (test 9b); if it were ever removed, the
# cached-vs-fresh comparisons would start catching this.
UNOBSERVABLE = [
    "the resolved path is recomputed instead of reused (root is already resolved)",
]


def main() -> int:
    files = sorted({s[1] for s in SABOTAGES})
    originals = {f: read(f) for f in files}
    hashes = {f: sha(f) for f in files}

    print("control run (unmodified tree)")
    if not suite_passes():
        print("  ABORT: the suite is not green before sabotage")
        return 1
    print("  suite is GREEN\n")

    caught = 0
    for label, path, old, new in SABOTAGES:
        s = originals[path]
        if s.count(old) != 1:
            print(f"  SKIP   {label}  -- anchor matched {s.count(old)} times, nothing written")
            continue
        write(path, s.replace(old, new, 1))
        try:
            red = not suite_passes()
        finally:
            write(path, originals[path])
        if red:
            caught += 1
            print(f"  CAUGHT {label}")
        else:
            print(f"  MISSED {label}")

    for f in files:
        write(f, originals[f])
        assert sha(f) == hashes[f], f"{f} not restored byte-identically"
    print("\nall files restored byte-identically")

    print("post-restore control")
    if not suite_passes():
        print("  ABORT: suite is red after restore")
        return 1
    print("  suite is GREEN")

    for label in UNOBSERVABLE:
        print(f"  N/A    {label}  -- no-op by construction, see UNOBSERVABLE")
    print(f"\n{caught}/{len(SABOTAGES)} sabotages detected"
          f"  ({len(UNOBSERVABLE)} unobservable by construction)")
    return 0 if caught == len(SABOTAGES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
