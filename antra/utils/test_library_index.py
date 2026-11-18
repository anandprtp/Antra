"""
Behavioural tests for the v2.0.0 SF-4 persistent library identity index.

These assert on OBSERVED EFFECT, never on source text:
  - what the built `_identity_index` dict actually contains,
  - how many files mutagen actually re-parsed (counted by wrapping the real
    extractor), which is the only thing that distinguishes a cache hit from a
    cache that silently does nothing.

The load-bearing property is that a cache hit must be INDISTINGUISHABLE from a
real read. Every test therefore compares against a `library_index_enabled=False`
build of the same library rather than against a hand-written expectation.

Run:  python antra/utils/test_library_index.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutagen.flac import FLAC  # noqa: E402

from antra.utils import organizer as organizer_mod  # noqa: E402
from antra.utils.organizer import LibraryOrganizer  # noqa: E402
from antra.utils.library_index import (  # noqa: E402
    LibraryIndex,
    compute_logic_version,
    get_library_index,
)
from antra.utils.runtime import get_ffmpeg_exe  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


# ── fixtures ──────────────────────────────────────────────────────────────

_SEED: Path | None = None


def _seed_flac(tmp: Path) -> Path:
    """One real ffmpeg-encoded FLAC, reused as the base for every fixture file."""
    global _SEED
    if _SEED is not None and _SEED.exists():
        return _SEED
    seed = tmp / "_seed.flac"
    subprocess.run(
        [get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", str(seed)],
        capture_output=True, check=True,
    )
    _SEED = seed
    return seed


def make_library(tmp: Path, name: str, n_albums: int = 2, n_tracks: int = 3) -> Path:
    seed = _seed_flac(tmp)
    root = tmp / name
    for a in range(n_albums):
        d = root / f"Artist {a}" / f"2020 - Album {a}"
        d.mkdir(parents=True, exist_ok=True)
        for t in range(1, n_tracks + 1):
            p = d / f"{t:02d} - Track {a}-{t}.flac"
            shutil.copyfile(seed, p)
            f = FLAC(p)
            f["title"] = f"Track {a}-{t}"
            f["artist"] = [f"Artist {a}"]
            f["album"] = f"Album {a}"
            f["isrc"] = f"US{a:04d}{t:06d}"
            f["tracknumber"] = str(t)
            f.save()
    return root


class CountingOrganizer(LibraryOrganizer):
    """Counts real tag reads so a hit can be told apart from a no-op cache."""

    def __init__(self, *a, **kw):
        self.reads = 0
        super().__init__(*a, **kw)

    def _extract_identity_keys_from_file(self, path):
        self.reads += 1
        return super()._extract_identity_keys_from_file(path)


def build(root: Path, db: Path | None, **kw) -> CountingOrganizer:
    if db is None:
        return CountingOrganizer(str(root), library_index_enabled=False, **kw)
    return CountingOrganizer(str(root), library_index_db_path=str(db), **kw)


# ── tests ─────────────────────────────────────────────────────────────────

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="antra_sf4_"))
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def run_all(tmp: Path) -> None:
    db = tmp / "index.db"

    # ── 1. a hit produces exactly the pre-SF-4 result, with zero reads ────
    print("\n[1] cache hit is indistinguishable from a real read")
    root = make_library(tmp, "lib1", 2, 3)
    baseline = build(root, None)
    check("baseline reads every file", baseline.reads == 6, f"reads={baseline.reads}")
    check("baseline built a non-empty index", len(baseline._identity_index) > 0)

    cold = build(root, db)
    check("cold run reads every file", cold.reads == 6, f"reads={cold.reads}")
    check("cold index == baseline index", cold._identity_index == baseline._identity_index)

    warm = build(root, db)
    check("warm run reads ZERO files", warm.reads == 0, f"reads={warm.reads}")
    check("warm index == baseline index", warm._identity_index == baseline._identity_index)

    # resolved paths must be byte-identical, not merely equivalent
    check(
        "warm resolved paths identical",
        sorted(set(warm._identity_index.values()))
        == sorted(set(baseline._identity_index.values())),
    )

    # ── 2. stat change re-reads that file only ───────────────────────────
    print("\n[2] a changed file is re-read; its neighbours are not")
    victim = next(root.rglob("*.flac"))
    f = FLAC(victim)
    f["title"] = "Retitled Track"
    f.save()  # changes both mtime and size
    after = build(root, db)
    check("exactly one file re-read", after.reads == 1, f"reads={after.reads}")
    fresh = build(root, None)
    check("index matches a full no-cache rebuild", after._identity_index == fresh._identity_index)
    check("new title is in the index", any("retitled track" in k for k in after._identity_index))

    # mtime-only change (same size) must also invalidate
    print("\n[2b] mtime-only change (identical size) still invalidates")
    build(root, db)  # re-prime
    t_new = time.time() + 120
    os.utime(victim, (t_new, t_new))
    touched = build(root, db)
    check("touched file re-read", touched.reads == 1, f"reads={touched.reads}")

    # ── 3. deleted file leaves the index and the DB ──────────────────────
    print("\n[3] a deleted file disappears from the index and is pruned")
    build(root, db)
    doomed = next(root.rglob("*.flac"))
    doomed_keys = set(build(root, None)._identity_index)
    doomed.unlink()
    gone = build(root, db)
    check("deleted file not in index values",
          not any(str(doomed) == v for v in gone._identity_index.values()))
    check("index matches a no-cache rebuild after deletion",
          gone._identity_index == build(root, None)._identity_index)
    idx = LibraryIndex(db)
    rows = idx.load_root(str(root.resolve()), gone._index_version)
    check("pruned row removed from the DB", str(doomed) not in rows)
    check("surviving rows still cached", len(rows) == 5, f"rows={len(rows)}")
    check("sanity: the deletion actually changed the key set",
          doomed_keys != set(gone._identity_index))

    # ── 4. a logic change invalidates every row ──────────────────────────
    print("\n[4] a change to the key-derivation logic invalidates the cache")
    build(root, db)
    v_now = compute_logic_version(organizer_mod._IDENTITY_LOGIC_FUNCTIONS)

    class Tweaked(LibraryOrganizer):
        @staticmethod
        def _normalize_identity_part(value):  # different source => different hash
            return LibraryOrganizer._normalize_identity_part(value)

    v_other = compute_logic_version(
        tuple(
            Tweaked._normalize_identity_part if getattr(fn, "__name__", "") == "_normalize_identity_part" else fn
            for fn in organizer_mod._IDENTITY_LOGIC_FUNCTIONS
        )
    )
    check("edited logic yields a different version", v_now != v_other, f"{v_now} vs {v_other}")
    stale = idx.load_root(str(root.resolve()), v_other)
    check("rows written under another version are not served", stale == {},
          f"served {len(stale)} rows")
    check("rows under the current version still are",
          len(idx.load_root(str(root.resolve()), v_now)) == 5)

    # ── 5. two libraries never contaminate each other ────────────────────
    print("\n[5] roots are isolated")
    root2 = make_library(tmp, "lib2", 1, 2)
    a = build(root, db)
    b = build(root2, db)
    check("second root reads its own files", b.reads == 2, f"reads={b.reads}")
    check("second root's index == its no-cache build",
          b._identity_index == build(root2, None)._identity_index)
    a2 = build(root, db)
    check("first root still fully cached", a2.reads == 0, f"reads={a2.reads}")
    check("first root's index unchanged", a2._identity_index == a._identity_index)

    # ── 6. full_albums mode is untouched ─────────────────────────────────
    print("\n[6] full_albums mode skips the index entirely (pre-SF-4 behaviour)")
    fa = build(root, db, full_albums=True)
    check("full_albums reads nothing", fa.reads == 0, f"reads={fa.reads}")
    check("full_albums builds no identity index", fa._identity_index == {})
    check("full_albums holds no index handle", fa._library_index is None)

    # ── 7. a broken DB degrades to the old behaviour ─────────────────────
    print("\n[7] an unusable database degrades to reading every file")
    # The db path IS a directory, so sqlite3.connect() cannot open it. (A
    # merely-missing file is not a failure — it is a cold cache, which is what
    # an earlier version of this test was accidentally measuring.)
    bad = tmp / "not_a_db"
    bad.mkdir()
    degraded = build(root, bad)
    check("degraded run still reads every file", degraded.reads == 5, f"reads={degraded.reads}")
    check("degraded index == no-cache index",
          degraded._identity_index == build(root, None)._identity_index)
    check("degraded run holds no index handle", degraded._library_index is None)

    print("\n[7b] a corrupt payload is treated as a miss, not as empty keys")
    build(root, db)
    idx2 = LibraryIndex(db)
    with idx2._lock:
        idx2._db.execute(
            "UPDATE library_files SET keys = ? WHERE root = ?",
            ("{not json", str(root.resolve())),
        )
        idx2._db.commit()
    corrupt = build(root, db)
    check("corrupt rows force a re-read", corrupt.reads == 5, f"reads={corrupt.reads}")
    check("corrupt-row index == no-cache index",
          corrupt._identity_index == build(root, None)._identity_index)

    # ── 8. vanished roots are cleaned up ─────────────────────────────────
    print("\n[8] rows for a library that no longer exists are dropped")
    organizer_mod._PRUNED_ROOTS_ONCE = False
    build(root2, db)
    idx3 = LibraryIndex(db)
    with idx3._lock:
        before = idx3._db.execute(
            "SELECT COUNT(*) FROM library_files WHERE root = ?", (str(root2.resolve()),)
        ).fetchone()[0]
    check("lib2 rows present before deletion", before > 0, f"rows={before}")
    shutil.rmtree(root2)
    dropped = idx3.prune_missing_roots()
    check("prune_missing_roots dropped the vanished root", dropped >= 1, f"dropped={dropped}")
    with idx3._lock:
        after_rows = idx3._db.execute(
            "SELECT COUNT(*) FROM library_files WHERE root = ?", (str(root2.resolve()),)
        ).fetchone()[0]
        kept = idx3._db.execute(
            "SELECT COUNT(*) FROM library_files WHERE root = ?", (str(root.resolve()),)
        ).fetchone()[0]
    check("vanished root's rows gone", after_rows == 0, f"rows={after_rows}")
    check("surviving root untouched by the sweep", kept > 0, f"rows={kept}")

    # ── 9. config → organizer wiring ─────────────────────────────────────
    print("\n[9] the setting reaches the organizer")
    import antra.core.config as config_mod
    cfg = config_mod.load_config()
    check("library_index_enabled defaults on", getattr(cfg, "library_index_enabled", None) is True)
    old = os.environ.get("LIBRARY_INDEX_ENABLED")
    os.environ["LIBRARY_INDEX_ENABLED"] = "false"
    try:
        check("env kill switch is honoured",
              config_mod.load_config().library_index_enabled is False)
    finally:
        if old is None:
            os.environ.pop("LIBRARY_INDEX_ENABLED", None)
        else:
            os.environ["LIBRARY_INDEX_ENABLED"] = old

    import antra.json_cli as jc

    class Cfg:
        pass
    c = Cfg()
    for k, v in vars(cfg).items():
        setattr(c, k, v)
    c.output_dir = str(root)
    c.library_index_enabled = False
    check("json_cli honours enabled=False", jc._make_organizer(c)._library_index is None)
    c.library_index_enabled = True
    c.library_index_db_path = str(db)
    org = jc._make_organizer(c)
    check("json_cli honours enabled=True", org._library_index is not None)
    check("json_cli passes the db path through",
          org._library_index is get_library_index(str(db)))

    # -- 9b. invariants the cache leans on --------------------------------
    print("\n[9b] the invariants that make a cached hit safe")
    org2 = build(root, db)
    check("organizer root is canonical (str(p) == str(p.resolve()) for every file)",
          org2.root == org2.root.resolve(), f"{org2.root}")
    sample = next(root.rglob("*.flac"))
    check("so a cached resolved path cannot differ from a freshly computed one",
          str(sample) == str(sample.resolve()))
    # A non-canonical root must still produce a correct index.
    weird = root.parent / root.name / ".." / root.name
    check("non-canonical root yields the same index",
          build(weird, db)._identity_index == build(root, None)._identity_index)

    # -- 9c. load_root is scoped to one root ------------------------------
    print("\n[9c] load_root is scoped to one root")
    root3 = make_library(tmp, "lib3", 1, 2)
    build(root3, db)
    build(root, db)
    idx4 = LibraryIndex(db)
    only = idx4.load_root(str(root.resolve()), org2._index_version)
    check("no other root's paths leak into the result",
          all(str(root.resolve()) in k for k in only), f"{len(only)} rows")
    check("lib3's own rows are still retrievable",
          len(idx4.load_root(str(root3.resolve()), org2._index_version)) == 2)

    # ── 10. the index is a cache, not a source of truth ──────────────────
    print("\n[10] cached keys never outlive the file they came from")
    build(root, db)
    survivor = next(root.rglob("*.flac"))
    survivor_resolved = str(survivor.resolve())
    check("survivor is in the cached index",
          survivor_resolved in build(root, db)._identity_index.values())
    survivor.unlink()
    after_del = build(root, db)
    check("removed file's path is not served from cache",
          survivor_resolved not in after_del._identity_index.values())


if __name__ == "__main__":
    raise SystemExit(main())
