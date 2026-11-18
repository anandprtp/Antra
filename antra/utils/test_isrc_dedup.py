"""
BUG-DUPE-1 — two DIFFERENT recordings sharing a title+artist were deduped.

Reported on the website against the Qobuz album `Stars` by Anne Wilson
(https://www.qobuz.com/us-en/album/stars-anne-wilson/emospe4d2uebu): tracks
15/16/17 are re-cuts of tracks 11/8/2 and carry the SAME title and artist, with
different ISRCs. `is_already_downloaded` matched on the fuzzy
`title_artist:` key and skipped them as "already downloaded".

The fix is an ISRC veto: two catalogue ISRCs that disagree are proof of two
different recordings, and that proof outranks a fuzzy title match. Anything
unknown on either side must stay silent — "I could not tell" is not
"different", or an untagged file becomes grounds for a second copy
(v1.1.8 BUG-3).

Every assertion runs through the REAL LibraryOrganizer against real files on
disk. The tracklist below is the live Qobuz mirror payload for that album.

Run: python antra/utils/test_isrc_dedup.py
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from antra.core.models import TrackMetadata
from antra.utils import runtime
from antra.utils.organizer import LibraryOrganizer
from mutagen.flac import FLAC

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  x " + label + (("\n      " + str(detail)) if detail else ""))


def eq(label, got, want):
    check(label, got == want, "got %r want %r" % (got, want))


_FIXTURE: list = []


def fixture() -> pathlib.Path:
    if not _FIXTURE:
        out = pathlib.Path(tempfile.mkdtemp(prefix="dupe_fx_")) / "src.flac"
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "anoisesrc=d=2:c=pink:r=44100:a=0.3:s=1", "-ac", "2",
             "-c:a", "flac", str(out)],
            check=True,
        )
        _FIXTURE.append(out)
    return _FIXTURE[0]


def write_audio(dest: str, t: TrackMetadata) -> None:
    """A real FLAC carrying this track's identity tags, ISRC included.

    Stub bytes would make mutagen raise, the identity scan return nothing, and
    the cross-run assertions below fail for a reason unrelated to the fix.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(fixture(), dest)
    a = FLAC(dest)
    a["title"] = t.title
    a["artist"] = list(t.artists or [])
    a["album"] = t.album or ""
    if t.isrc:
        a["isrc"] = t.isrc
    if t.track_number:
        a["tracknumber"] = str(t.track_number)
    a.save()


# ── the reported album, verbatim from https://qobuz.anandserver.cfd/api/album/
#    emospe4d2uebu on 2026-09-27. Tracks 15/16/17 repeat 11/8/2 by title.
ALBUM = "Stars"
ARTIST = "Anne Wilson"
TRACKS = [
    (1, "Stars", "USUM72509124"),
    (2, "God Story", "USUM72505574"),
    (8, "Carry Me", "USUM72509133"),
    (11, "Still Do", "USUM72509139"),
    (15, "Still Do", "USUM72601175"),
    (16, "Carry Me", "USUM72605494"),
    (17, "God Story", "USUM72605496"),
]


def track(number, title, isrc, album=ALBUM, artist=ARTIST):
    return TrackMetadata(
        title=title, artists=[artist], album=album, album_artists=[artist],
        track_number=number, disc_number=1, total_tracks=17, isrc=isrc,
        release_year=2025,
    )


def run_album(root, tracks, tagged=True):
    """Download the album through the real organizer. Returns skipped tracks."""
    org = LibraryOrganizer(root)
    skipped = []
    for t in tracks:
        existing = org.is_already_downloaded(t)
        if existing:
            skipped.append((t.track_number, t.title, t.isrc))
            continue
        path = org.get_output_path(t) + ".flac"
        if tagged:
            write_audio(path, t)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(b"x")
        org.mark_downloaded(t, path)
    return org, skipped


def tmp():
    return tempfile.mkdtemp(prefix="dupe_")


print("BUG-DUPE-1 — ISRC veto on dedup")

# ── 1. the reported bug, one run ──────────────────────────────────────────
root = tmp()
org, skipped = run_album(root, [track(*t) for t in TRACKS])
eq("the re-cuts are downloaded, not skipped", skipped, [])
files = sorted(p.name for p in pathlib.Path(root).rglob("*.flac"))
eq("all 7 files on disk", len(files), 7)
check("both 'Still Do' recordings exist",
      sum("Still Do" in f for f in files) == 2, files)

# ── 2. CONTROL — real duplicates must STILL be skipped ────────────────────
# Without this the fix could simply have disabled dedup.
root = tmp()
same = track(11, "Still Do", "USUM72509139")
org, skipped = run_album(root, [same, track(11, "Still Do", "USUM72509139")])
eq("CONTROL: the SAME ISRC twice is still deduped",
   [s[2] for s in skipped], ["USUM72509139"])

# ── 3. CONTROL — no ISRC at all: unchanged fuzzy dedup ────────────────────
root = tmp()
org, skipped = run_album(root, [
    track(11, "Still Do", None),
    track(15, "Still Do", None),
])
eq("CONTROL: with no ISRC on either side the fuzzy key still dedupes",
   len(skipped), 1)

# ── 4. CONTROL — one side unknown is NOT proof ────────────────────────────
root = tmp()
org, skipped = run_album(root, [
    track(11, "Still Do", None),
    track(15, "Still Do", "USUM72601175"),
])
eq("CONTROL: ours known, theirs unknown -> still deduped (no proof)",
   len(skipped), 1)

root = tmp()
org, skipped = run_album(root, [
    track(11, "Still Do", "USUM72509139"),
    track(15, "Still Do", None),
])
eq("CONTROL: ours unknown, theirs known -> still deduped (no proof)",
   len(skipped), 1)

# ── 5. cross-RUN: the veto must survive a fresh organizer reading tags ────
# The in-run case is answered from memory; this one can only be answered by
# reading the ISRC back out of the file, which is the other half of the fix.
root = tmp()
run_album(root, [track(11, "Still Do", "USUM72509139")])
org2 = LibraryOrganizer(root)   # fresh: index rebuilt from disk
got = org2.is_already_downloaded(track(15, "Still Do", "USUM72601175"))
eq("cross-run: the re-cut is not skipped against the tagged studio file", got, None)
got = org2.is_already_downloaded(track(11, "Still Do", "USUM72509139"))
check("CONTROL cross-run: the same ISRC IS found on disk", bool(got), got)

# ── 5b. in-run with UNTAGGED files — only the in-run memory can answer ───
# The engine writes the file before tagging on some paths, so the veto must not
# depend on the file already carrying its ISRC.
root = tmp()
org, skipped = run_album(root, [
    track(11, "Still Do", "USUM72509139"),
    track(15, "Still Do", "USUM72601175"),
], tagged=False)
eq("in-run, untagged on disk: the re-cut is still not skipped", skipped, [])

# ── 5c. a tagged file that the organizer has never seen (no state) ───────
# A library filled by an earlier version, or by another tool. The ISRC has to
# come off the file itself here — there is no state entry to read it from.
root = tmp()
studio = track(11, "Still Do", "USUM72509139")
org5 = LibraryOrganizer(root)
write_audio(org5.get_output_path(studio) + ".flac", studio)
org6 = LibraryOrganizer(root)            # fresh: sees the file only by scanning
eq("unseen-but-tagged library file: the re-cut is not skipped",
   org6.is_already_downloaded(track(15, "Still Do", "USUM72601175")), None)
check("CONTROL: the same recording IS found in that same library",
      bool(org6.is_already_downloaded(studio)))

# ── 6. cross-run, file UNTAGGED but recorded in the organizer's state ────
# Written expecting "cannot tell"; measured otherwise, and the measurement is
# right. `.antra_state` maps `isrc:<x>` -> path, so the organizer knows that
# path's ISRC even when the file itself carries no tags at all. That is real
# knowledge, so the veto is entitled to use it.
root = tmp()
run_album(root, [track(11, "Still Do", "USUM72509139")], tagged=False)
org3 = LibraryOrganizer(root)
stub = str(next(pathlib.Path(root).rglob("*.flac")))
eq("untagged file: the ISRC is recovered from .antra_state",
   org3._known_isrc_for_path(stub), "USUM72509139")
eq("untagged-but-known file: the re-cut is still not skipped",
   org3.is_already_downloaded(track(15, "Still Do", "USUM72601175")), None)

# ── 6b. genuinely unknown file — the veto must stay silent ───────────────
root = tmp()
stray = os.path.join(root, "stray.flac")
os.makedirs(root, exist_ok=True)
open(stray, "wb").write(b"x")            # unreadable by mutagen, no state entry
org3b = LibraryOrganizer(root)
eq("CONTROL: an unreadable file with no state entry reports 'cannot tell'",
   org3b._is_positively_different_recording(
       track(15, "Still Do", "USUM72601175"), stray),
   False)

# ── 7. the veto is ISRC-only: a different ALBUM still dedupes ─────────────
# The same recording on a Best Of must still be found by the cross-library
# index — that is what smart_dedup is for.
root = tmp()
run_album(root, [track(11, "Still Do", "USUM72509139")])
org4 = LibraryOrganizer(root)
got = org4.is_already_downloaded(
    track(3, "Still Do", "USUM72509139", album="Greatest Hits"))
check("CONTROL: same recording on another album is still deduped", bool(got), got)

# ── 8. unit-level: the two helpers ────────────────────────────────────────
o = LibraryOrganizer(tmp())
eq("_isrc_from_identity_keys picks the isrc key",
   o._isrc_from_identity_keys(["title_artist:a:b", "isrc:usum72509139"]),
   "USUM72509139")
eq("_isrc_from_identity_keys with no isrc key", o._isrc_from_identity_keys([]), None)
eq("_isrcs_disagree is case/whitespace insensitive",
   o._isrcs_disagree(track(1, "x", " usum72509139 "), "USUM72509139"), False)
eq("_isrcs_disagree on two different ISRCs",
   o._isrcs_disagree(track(1, "x", "USUM72509139"), "USUM72601175"), True)
eq("_isrcs_disagree stays silent when ours is missing",
   o._isrcs_disagree(track(1, "x", None), "USUM72601175"), False)
eq("_isrcs_disagree stays silent when theirs is missing",
   o._isrcs_disagree(track(1, "x", "USUM72509139"), None), False)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
