"""AUDIT-DESKTOP-14 — sidecar writers: invalid cue numbering, double-counted
coverage, truncating writes.

  1. build_cue used each track's own track_number, so a playlist folder (tracks
     from different albums, all "1") produced `TRACK 01` repeatedly: not a valid
     cue sheet.
  2. group_entries did not dedupe by path, so a track listed twice (the second a
     SKIPPED pointing at the same file) counted twice toward the coverage rule,
     letting a run that accounts for ONE of two files overwrite a good sidecar.
  3. Sidecars were written with a truncating open(): a title containing a lone
     surrogate (JSON can produce one) raised UnicodeEncodeError - not an OSError,
     so write_sidecars did not catch it - AFTER the existing file had been
     truncated; the cover/lyrics writers truncate in place the same way, so a
     kill mid-write left a partial cover that the "a cover already exists" guard
     then kept forever.

Real functions, real files in a temp dir.   Run: python antra/utils/test_sidecar_writers_audit.py
"""
from __future__ import annotations

import builtins
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.utils import sidecars as sc  # noqa: E402
from antra.utils import tagger as tg  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


TMP = Path(tempfile.mkdtemp(prefix="scaudit_"))


def trk(title, n=1, disc=1, **kw):
    base = dict(title=title, artists=["A"], album_artists=["A"], album="Al", track_number=n, disc_number=disc,
                duration_ms=180000, isrc=None, release_year=2020, genres=[], playlist_name=None, total_discs=1)
    base.update(kw)
    return SimpleNamespace(**base)


def touch(d, name):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x")
    return p


def cue_nums(tracks):
    return [ln.split()[1] for ln in sc.build_cue(tracks, 1, 1).splitlines() if ln.strip().startswith("TRACK")]


# 1. cue numbering
d = TMP / "Playlist"
files = [touch(d, f"{i}.flac") for i in range(3)]
check("playlist cue: TRACK numbers are unique and ascending",
      cue_nums([(trk(f"S{i}", n=1), p) for i, p in enumerate(files)]) == ["01", "02", "03"],
      cue_nums([(trk(f"S{i}", n=1), p) for i, p in enumerate(files)]))
check("control: real, unique numbers are kept",
      cue_nums([(trk(f"S{i}", n=n), p) for (i, p), n in zip(enumerate(files), (1, 2, 3))]) == ["01", "02", "03"])
check("control: ascending real numbers with gaps (a partial album) are kept",
      cue_nums([(trk(f"S{i}", n=n), p) for (i, p), n in zip(enumerate(files), (1, 3, 7))]) == ["01", "03", "07"])
check("ascending but out-of-range numbers (150, 300) fall back to positions",
      cue_nums([(trk("a", n=150), files[0]), (trk("b", n=300), files[1])]) == ["01", "02"],
      cue_nums([(trk("a", n=150), files[0]), (trk("b", n=300), files[1])]))

# 2. coverage must count FILES, not entries
d2 = TMP / "Album"
a, b = touch(d2, "a.flac"), touch(d2, "b.flac")
kept = sc.filter_to_covered(sc.group_entries([(trk("A"), a), (trk("A again"), a)]))   # b.flac unaccounted for
check("a file listed twice does not satisfy the coverage rule for a folder holding two", kept == {}, list(kept))
kept = sc.filter_to_covered(sc.group_entries([(trk("A"), a), (trk("B", 2), b)]))
check("control: both files accounted for -> covered", len(kept) == 1 and len(next(iter(kept.values()))) == 2, kept)
g = sc.group_entries([(trk("A"), a), (trk("A again"), a), (trk("B", 2), b)])
check("control: the duplicate is collapsed, first occurrence kept",
      [t.title for t, _ in next(iter(g.values()))] == ["A", "B"], [t.title for t, _ in next(iter(g.values()))])

# 3. sidecar writes
target = TMP / "Release"
target.mkdir()
existing = target / "Release.nfo"
existing.write_bytes(b"GOOD EXISTING NFO")
bad = sc.SidecarFile(str(existing), "title \ud800 broken", "nfo")
other = sc.SidecarFile(str(target / "Release.json"), "{}\n", "json")
try:
    written = sc.write_sidecars([bad, other])
    err = None
except Exception as e:
    written, err = None, f"{type(e).__name__}: {e}"
check("an unencodable sidecar does not raise out of write_sidecars", err is None, err)
check("...and the existing good file is left intact", existing.read_bytes() == b"GOOD EXISTING NFO", existing.read_bytes())
check("...and the other planned file is still written", (target / "Release.json").exists(), written)
check("...and no temp file is left behind", not [p.name for p in target.iterdir() if "tmp" in p.name.lower()],
      [p.name for p in target.iterdir()])
ok = sc.write_sidecars([sc.SidecarFile(str(existing), "new content", "nfo")])
check("control: a normal rewrite replaces the file", ok and existing.read_bytes() == b"new content", existing.read_bytes())

# the tagger's image / lyrics writers
tagger = tg.FileTagger.__new__(tg.FileTagger)
img_dir = TMP / "Cover"
img_dir.mkdir()
cover = img_dir / "cover.jpg"
cover.write_bytes(b"OLD-COVER")
real_open = builtins.open


def failing_open(path, mode="r", *a, **k):
    f = real_open(path, mode, *a, **k)
    if "w" in mode and "b" in mode and str(path).startswith(str(img_dir)):
        orig_write = f.write

        def w(data):
            orig_write(data[: len(data) // 2])
            raise OSError("disk full")
        f.write = w
    return f


builtins.open = failing_open
try:
    tagger._write_image_sidecars(str(img_dir), "cover", [(b"NEW-COVER-DATA" * 10, "image/jpeg")], str(img_dir))
finally:
    builtins.open = real_open
check("a cover write failing part-way leaves the previous cover intact", cover.read_bytes() == b"OLD-COVER", cover.read_bytes()[:30])
check("...and leaves no partial/temp file", sorted(p.name for p in img_dir.iterdir()) == ["cover.jpg"], [p.name for p in img_dir.iterdir()])
res = tagger._write_image_sidecars(str(img_dir), "cover2", [(b"FRESH", "image/jpeg")], str(img_dir))
check("control: a normal cover write works", (img_dir / "cover2.jpg").read_bytes() == b"FRESH", res)

ogg = TMP / "t.ogg"
ogg.write_bytes(b"x")
lrc = TMP / "t.lrc"
lrc.write_text("OLD LRC", encoding="utf-8")
txt = TMP / "t.txt"
txt.write_text("OLD TXT", encoding="utf-8")
tagger._want = lambda k: True
tagger._write_lyrics_sidecars(str(ogg), SimpleNamespace(synced_lyrics="new \ud800 bad", lyrics=None))
check("a synced-lyrics sidecar that cannot be encoded does not destroy the existing .lrc",
      lrc.read_text(encoding="utf-8") == "OLD LRC", lrc.read_bytes())
tagger._write_lyrics_sidecars(str(ogg), SimpleNamespace(synced_lyrics=None, lyrics="plain \ud800 bad"))
check("a plain-lyrics sidecar that cannot be encoded does not destroy the existing .txt",
      txt.read_text(encoding="utf-8") == "OLD TXT", txt.read_bytes())
tagger._write_lyrics_sidecars(str(ogg), SimpleNamespace(synced_lyrics="[00:01.00]fine", lyrics="plain fine"))
check("control: normal lyrics sidecars are written",
      lrc.read_text(encoding="utf-8") == "[00:01.00]fine" and txt.read_text(encoding="utf-8") == "plain fine")

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
