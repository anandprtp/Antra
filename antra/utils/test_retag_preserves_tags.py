"""Re-tagging must never erase a good tag with an empty value.

A tag_only re-tag (DMX-5) runs the writers over a user's existing file with whatever
metadata the fetcher produced this time. `album` was written unconditionally, so a
track whose album came back empty wiped "Good Album" from FLAC and M4A (set to "") and
from MP3 (frame removed). Real files, real mutagen.

Run:  python antra/utils/test_retag_preserves_tags.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mutagen import File

from antra.core.models import TrackMetadata
from antra.utils.runtime import get_ffmpeg_exe
from antra.utils.tagger import FileTagger

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


FF = get_ffmpeg_exe()
assert FF, "ffmpeg is required"
D = tempfile.mkdtemp(prefix="retag")


def make(ext, codec):
    p = os.path.join(D, f"t{ext}")
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=2:r=44100",
                    "-ac", "2", "-c:a", codec, p], check=True)
    return p


def tags(p):
    t = dict(File(p, easy=True).tags or {})
    return {k: v for k, v in t.items() if k in ("title", "artist", "album", "albumartist")}


good = TrackMetadata(title="Song", artists=["Band"], album="Good Album", album_artists=["Band"],
                     release_year=2020, track_number=1, total_tracks=10)
tg = FileTagger()

for ext, codec in ((".flac", "flac"), (".mp3", "libmp3lame"), (".m4a", "aac")):
    p = make(ext, codec)
    check(f"{ext}: tagging succeeds", tg.tag(p, good, provenance=False))
    before = tags(p)
    check(f"{ext}: control - initial tags written", before.get("album") == ["Good Album"]
          and before.get("title") == ["Song"] and before.get("artist") == ["Band"])

    # the bug: a retag whose metadata has no album / album artist
    empty = TrackMetadata(title="Song", artists=["Band"], album="", album_artists=[],
                          release_year=2020, track_number=1)
    check(f"{ext}: retag with empty album succeeds", tg.tag(p, empty, provenance=False))
    after = tags(p)
    check(f"{ext}: empty album does not erase the existing album", after.get("album") == ["Good Album"])
    check(f"{ext}: title/artist untouched", after.get("title") == ["Song"] and after.get("artist") == ["Band"])

    # control: a real new value must still overwrite (the fix must not make tags read-only)
    newer = TrackMetadata(title="Song", artists=["Band"], album="Remastered Album",
                          album_artists=["Band"], release_year=2020, track_number=1)
    tg.tag(p, newer, provenance=False)
    check(f"{ext}: a non-empty album still overwrites", tags(p).get("album") == ["Remastered Album"])

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
