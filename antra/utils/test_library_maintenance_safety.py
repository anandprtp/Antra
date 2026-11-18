"""library_maintenance (CLI dedupe) must not delete songs that are not duplicates.

Real tagged files. Covers: different artists sharing a title, non-ASCII titles that the old
normaliser collapsed to the same string, keeping the better copy, and not removing the
library root once it is empty.

Run:  python antra/utils/test_library_maintenance_safety.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TSRC

from antra.utils import library_maintenance as lm
from antra.utils.runtime import get_ffmpeg_exe

FF = get_ffmpeg_exe()
assert FF, "ffmpeg is required"
FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


def make(path: Path, title: str, artist: str, album: str, isrc: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    flac = path.suffix == ".flac"
    codec = ["-c:a", "flac"] if flac else ["-c:a", "libmp3lame", "-b:a", "64k"]
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=1:r=22050",
                    "-ac", "1", *codec, str(path)], check=True)
    if flac:
        a = FLAC(str(path))
        a["title"], a["artist"], a["album"] = title, artist, album
        if isrc:
            a["isrc"] = isrc
        a.save()
    else:
        t = ID3()
        t.add(TIT2(encoding=3, text=title))
        t.add(TPE1(encoding=3, text=artist))
        t.add(TALB(encoding=3, text=album))
        if isrc:
            t.add(TSRC(encoding=3, text=isrc))
        t.save(str(path))
    return path


def lib():
    return Path(tempfile.mkdtemp(prefix="lm"))


# ── 1. two different artists, same title: NOT duplicates ────────────────────
root = lib()
make(root / "Albums" / "Artist A" / "Album A (2020)" / "01 - Intro.flac", "Intro", "Artist A", "Album A")
make(root / "Albums" / "Artist B" / "Album B (2021)" / "01 - Intro.flac", "Intro", "Artist B", "Album B")
check("different artists sharing a title are not grouped", lm.find_duplicate_songs(str(root)) == [])
lm.dedupe_duplicate_songs(str(root))
check("nothing deleted for same-title/different-artist songs",
      len(list(root.rglob("*.flac"))) == 2)

# ── 2. non-ASCII titles: the old normaliser turned both into "2" ────────────
check("normalize keeps non-ASCII letters", lm._normalize("Привет 2") != lm._normalize("Пока 2"))
check("normalize still folds case/punctuation", lm._normalize("Hello,  WORLD!") == "hello world")
root = lib()
make(root / "Albums" / "Artist A" / "Al (2020)" / "01 - x.flac", "Привет 2", "Artist A", "Al")
make(root / "Albums" / "Artist A" / "Al (2020)" / "02 - y.flac", "Пока 2", "Artist A", "Al")
check("distinct non-ASCII titles are not grouped", lm.find_duplicate_songs(str(root)) == [])

# ── 3. controls: a real duplicate IS found, and the better copy is kept ─────
root = lib()
flac = make(root / "Albums" / "Artist A" / "Al (2020)" / "01 - Song.flac", "Song", "Artist A", "Al", "USAB12300001")
mp3 = make(root / "Playlists" / "Mix" / "Song.mp3", "Song", "Artist A", "Al", "USAB12300001")
os.utime(flac, (2_000_000_000, 2_000_000_000))      # the FLAC is NEWER, so mtime alone would keep the MP3
os.utime(mp3, (1_000_000_000, 1_000_000_000))
groups = lm.find_duplicate_songs(str(root))
check("control: a real duplicate pair is found", len(groups) == 1)
check("the lossless copy is chosen as canonical", groups and groups[0].canonical == flac.resolve())
lm.dedupe_duplicate_songs(str(root))
check("the MP3 duplicate is removed, the FLAC kept", flac.exists() and not mp3.exists())

# ── 4. emptying a library must not remove the library root ──────────────────
root = lib() / "MyMusic"
only = make(root / "Albums" / "Artist A" / "Al (2020)" / "01 - Song.mp3", "Song", "Artist A", "Al")
lm._delete_track_bundle(only, stop_at=root)
check("album/artist folders emptied", not (root / "Albums" / "Artist A").exists())
check("the library root itself survives", root.exists())
check("and so does its parent", root.parent.exists())
check("control: empty folders below the root are cleaned", not (root / "Albums").exists())

# ── 5. album dedupe never deletes a better file in favour of a worse one ────
root = lib()
make(root / "Albums" / "Artist A" / "Album (2020)" / "01 - Song.mp3", "Song", "Artist A", "Album", "USAB12300002")
better = make(root / "Albums" / "Artist A" / "Album" / "01 - Song.flac", "Song", "Artist A", "Album", "USAB12300002")
make(root / "Albums" / "Artist A" / "Album (2020)" / "02 - Other.mp3", "Other", "Artist A", "Album", "USAB12300003")
report = lm.dedupe_duplicate_albums(str(root))
check("album dedupe kept the better (FLAC) copy", better.exists())
check("album dedupe reports the track it kept for quality", report.tracks_kept_for_quality >= 1)

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
