"""
issue #75 — a playlist folder must stay in step with the playlist.

Three properties, all driven through the REAL LibraryOrganizer against real
files on disk:

  1. ensure_playlist_copy must not create a second copy when the track is
     already in the playlist folder under a different number. This is the
     reported bug: Spotify's Liked Songs prepends, every position shifts, and
     the folder grew by one file per track per run while dedup reported
     "already in library" the whole time.
  2. renumber_playlist_folder renames the files to their current positions,
     including the uniform +1 shift where every destination is occupied.
  3. find_playlist_orphans reports — and never deletes — files the playlist no
     longer contains, and is a no-op in library storage mode.

Run: python antra/utils/test_playlist_renumber.py
"""
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from antra.core.models import TrackMetadata
from antra.utils import runtime
from antra.utils.organizer import LibraryOrganizer, normalize_prune_mode
from mutagen.flac import FLAC

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"

# Real, TAGGED audio, not stub bytes. One of these assertions — reusing a copy
# whose number moved when the canonical file lives under its album — can only
# pass through _extract_identity_keys_from_file, which reads tags. With stub
# files mutagen raises, the folder index comes back empty and the assertion
# fails for a reason that has nothing to do with the code under test. Found by
# writing the stub version first.
_FIXTURE: list = []


def fixture() -> pathlib.Path:
    if not _FIXTURE:
        out = pathlib.Path(tempfile.mkdtemp(prefix="i75_fx_")) / "src.flac"
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "anoisesrc=d=2:c=pink:r=44100:a=0.3:s=1", "-ac", "2",
             "-c:a", "flac", str(out)],
            check=True,
        )
        _FIXTURE.append(out)
    return _FIXTURE[0]


def write_audio(dest: str, t: TrackMetadata) -> None:
    """Place a real FLAC carrying this track's identity tags."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(fixture(), dest)
    a = FLAC(dest)
    a["title"] = t.title
    a["artist"] = list(t.artists or [])
    a["album"] = t.album or ""
    if t.spotify_id:
        a["spotify_id"] = t.spotify_id
    a.save()

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  x {label}" + (f"\n      {detail}" if detail else ""))


def eq(label, got, want):
    check(label, got == want, f"got:  {got!r}\n      want: {want!r}")


PLAYLIST = "Liked Songs"


def track(pos, title, artist, sid):
    return TrackMetadata(
        title=title, artists=[artist], album=PLAYLIST, playlist_name=PLAYLIST,
        playlist_position=pos, spotify_id=sid, duration_ms=200_000,
    )


def organizer(root, **kw):
    kw.setdefault("album_track_filename_template", "{track} - {title} - {artist}")
    # The reporter's own template. library_index_enabled=False keeps the test
    # off the shared SF-4 sqlite cache — this suite is about the organizer.
    return LibraryOrganizer(root=root, library_index_enabled=False, **kw)


def names(root, folder=PLAYLIST):
    d = pathlib.Path(root, folder)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def run(org, tracks):
    """One sync pass, exactly as engine.download_track does it."""
    entries = []
    for t in tracks:
        existing = org.is_already_downloaded(t)
        if existing:
            entries.append((t, org.ensure_playlist_copy(t, existing)))
        else:
            p = org.get_output_path(t) + ".flac"
            write_audio(p, t)
            org.mark_downloaded(t, p)
            entries.append((t, p))
    return entries


SONGS = [("Alpha", "A1", "sid1"), ("Bravo", "A2", "sid2"), ("Charlie", "A3", "sid3")]


def seed(root, songs=SONGS, **kw):
    org = organizer(root, **kw)
    run(org, [track(i + 1, *s) for i, s in enumerate(songs)])
    return org


def shifted(songs, new_first=("NewSong", "A0", "sid0")):
    return [new_first] + list(songs)


# ══════════════════════════════════════════════════════════════════════════
# 1. THE REPORTED BUG — no duplicate when the position moved.
# ══════════════════════════════════════════════════════════════════════════
print("1. duplicates")
root = tempfile.mkdtemp(prefix="i75_dup_")
seed(root)
eq("first sync writes one file per track", names(root),
   ["01 - Alpha - A1.flac", "02 - Bravo - A2.flac", "03 - Charlie - A3.flac"])

songs2 = shifted(SONGS)
o2 = organizer(root, playlist_renumber=False)  # isolate the copy behaviour
run(o2, [track(i + 1, *s) for i, s in enumerate(songs2)])
eq("a song added at the top adds exactly ONE file", len(names(root)), 4)
check("the pre-existing files kept their original names",
      set(names(root)) >= {"01 - Alpha - A1.flac", "02 - Bravo - A2.flac", "03 - Charlie - A3.flac"},
      names(root))

# Unbounded growth was the reported symptom — one more run must add one more file.
songs3 = shifted(songs2, ("Newer", "A00", "sid00"))
o3 = organizer(root, playlist_renumber=False)
run(o3, [track(i + 1, *s) for i, s in enumerate(songs3)])
eq("a second shift adds exactly one more file", len(names(root)), 5)

# ── Control: the copy IS still made when the track lives elsewhere ────────
# Without this, "no duplicate" could be passing because ensure_playlist_copy
# does nothing at all, which would break the feature it exists for.
root_c = tempfile.mkdtemp(prefix="i75_ctl_")
oc = organizer(root_c)
album_track = TrackMetadata(title="Solo", artists=["A9"], album="Some Album",
                            track_number=4, spotify_id="sidsolo", duration_ms=200_000)
album_path = oc.get_output_path(album_track) + ".flac"
write_audio(album_path, album_track)
oc.mark_downloaded(album_track, album_path)
pl_track = track(1, "Solo", "A9", "sidsolo")
copied = oc.ensure_playlist_copy(pl_track, album_path)
check("a track filed under its album DOES get a playlist copy",
      os.path.exists(copied) and os.path.dirname(copied) != os.path.dirname(album_path), copied)
eq("and the playlist folder holds exactly that one file", names(root_c), ["01 - Solo - A9.flac"])

# ...and a SECOND run does not add another, even though its number moved.
pl_track2 = track(7, "Solo", "A9", "sidsolo")
oc2 = organizer(root_c, playlist_renumber=False)
again = oc2.ensure_playlist_copy(pl_track2, album_path)
eq("the moved-position copy is reused, not duplicated", names(root_c), ["01 - Solo - A9.flac"])
check("and the reused path is the one already on disk", os.path.exists(again), again)

# ══════════════════════════════════════════════════════════════════════════
# 2. RENUMBERING
# ══════════════════════════════════════════════════════════════════════════
print("2. renumbering")
root = tempfile.mkdtemp(prefix="i75_num_")
seed(root)
o = organizer(root)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))])
renamed = o.renumber_playlist_folder(PLAYLIST, entries)
eq("the uniform +1 shift renames every pre-existing file", len(renamed), 3)
eq("and the folder reads in playlist order", names(root),
   ["01 - NewSong - A0.flac", "02 - Alpha - A1.flac",
    "03 - Bravo - A2.flac", "04 - Charlie - A3.flac"])
check("no staging file survived", not any(n.endswith(".tmp") for n in names(root)), names(root))
eq("results point at the NEW paths",
   sorted(os.path.basename(p) for _, p in
          [(t, renamed.get(p, p)) for t, p in entries]),
   names(root))

# Dedup state must follow the rename ON THE SAME INSTANCE. Asserting only via a
# fresh organizer would prove nothing: the constructor rebuilds the index by
# scanning the folder, so it recovers from a missing _repath on its own and the
# sabotage goes undetected. Measured — this pair was added after the negative
# control missed it.
state_file = pathlib.Path(root, ".antra_state.json")
state_text = state_file.read_text(encoding="utf-8")
check("the state file names the NEW path", "02 - Alpha - A1.flac" in state_text)
check("and no longer names the old one", "01 - Alpha - A1.flac" not in state_text)
alpha_now = o.is_already_downloaded(track(2, *SONGS[0]))
check("the SAME organizer still resolves a renamed track to a file that exists",
      bool(alpha_now) and os.path.exists(alpha_now), str(alpha_now))

# Dedup state must follow the rename, or the next run re-downloads everything.
o_next = organizer(root)
for i, s in enumerate(shifted(SONGS)):
    t = track(i + 1, *s)
    found = o_next.is_already_downloaded(t)
    check(f"dedup still finds {s[0]} after the rename", bool(found) and os.path.exists(found),
          str(found))

eq("a second run with no changes renames nothing",
   len(o_next.renumber_playlist_folder(
       PLAYLIST, run(o_next, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))]))), 0)

# ── A full reversal: every destination is another source. ────────────────
root = tempfile.mkdtemp(prefix="i75_rev_")
seed(root)
o = organizer(root)
reversed_songs = list(reversed(SONGS))
entries = run(o, [track(i + 1, *s) for i, s in enumerate(reversed_songs)])
o.renumber_playlist_folder(PLAYLIST, entries)
eq("reversing the playlist renumbers cleanly", names(root),
   ["01 - Charlie - A3.flac", "02 - Bravo - A2.flac", "03 - Alpha - A1.flac"])

# ── REAL name collisions, which need the two-phase rename. ───────────────
#
# Every case above uses "{track} - {title} - {artist}", where a shift moves
# 01 - Alpha -> 02 - Alpha while 02 - Bravo sits there under a DIFFERENT stem —
# so nothing actually collides and a naive one-pass rename would pass. With a
# number-only template the destination of each rename is another file's current
# name, which is the case os.replace would silently overwrite.
# The negative control missed the one-phase sabotage until this existed.
NUMBERED = {"album_track_filename_template": "{track}"}
root = tempfile.mkdtemp(prefix="i75_col_")
seed(root, **NUMBERED)
eq("number-only template writes bare numbers", names(root), ["01.flac", "02.flac", "03.flac"])
o = organizer(root, **NUMBERED)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))])
o.renumber_playlist_folder(PLAYLIST, entries)
eq("a +1 shift where every destination is occupied still renumbers", names(root),
   ["01.flac", "02.flac", "03.flac", "04.flac"])
# Identity, not just the count: a one-pass rename overwrites and loses tracks.
survivors = sorted(FLAC(str(p)).get("title", [""])[0]
                   for p in pathlib.Path(root, PLAYLIST).iterdir())
eq("and every track survived the shift", survivors,
   ["Alpha", "Bravo", "Charlie", "NewSong"])
eq("with the new song first", FLAC(str(pathlib.Path(root, PLAYLIST, "01.flac")))["title"][0],
   "NewSong")

root = tempfile.mkdtemp(prefix="i75_cyc_")
seed(root, **NUMBERED)
o = organizer(root, **NUMBERED)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(reversed(SONGS))])
o.renumber_playlist_folder(PLAYLIST, entries)
eq("a reversal (a rename CYCLE) keeps all three files", names(root),
   ["01.flac", "02.flac", "03.flac"])
eq("and puts them in the new order",
   [FLAC(str(pathlib.Path(root, PLAYLIST, n)))["title"][0] for n in names(root)],
   ["Charlie", "Bravo", "Alpha"])

# ── Renumbering off leaves the names alone. ──────────────────────────────
root = tempfile.mkdtemp(prefix="i75_off_")
seed(root)
o = organizer(root, playlist_renumber=False)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))])
eq("playlist_renumber=False renames nothing",
   len(o.renumber_playlist_folder(PLAYLIST, entries)), 0)
check("and the stale numbers are still there", "01 - Alpha - A1.flac" in names(root), names(root))

# ── A name held by a file this run does not own is never overwritten. ────
root = tempfile.mkdtemp(prefix="i75_blk_")
seed(root)
o = organizer(root)
# Alpha wants "02 - Alpha - A1.flac" after the shift. Put an unrelated file
# there first; the rename must be skipped rather than clobber it.
blocker = pathlib.Path(root, PLAYLIST, "02 - Alpha - A1.flac")
blocker.write_bytes(b"NOT OURS")
entries = run(o, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))])
o.renumber_playlist_folder(PLAYLIST, entries)
eq("a blocked destination is left untouched", blocker.read_bytes(), b"NOT OURS")

# ══════════════════════════════════════════════════════════════════════════
# 3. ORPHANS
# ══════════════════════════════════════════════════════════════════════════
print("3. orphans")
root = tempfile.mkdtemp(prefix="i75_orp_")
seed(root)
kept = [s for s in SONGS if s[0] != "Bravo"]
o = organizer(root)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(kept)])
orphans = o.find_playlist_orphans(PLAYLIST, [p for _, p in entries])
eq("the dropped song's file is reported", [os.path.basename(p) for p in orphans],
   ["02 - Bravo - A2.flac"])
check("finding them deletes nothing", os.path.exists(orphans[0]))

removed = o.prune_playlist_orphans(orphans)
eq("pruning removes exactly those", [os.path.basename(p) for p in removed],
   ["02 - Bravo - A2.flac"])
check("the file is gone", not os.path.exists(orphans[0]))
check("and a file still in the playlist survives",
      os.path.exists(pathlib.Path(root, PLAYLIST, "01 - Alpha - A1.flac")), names(root))

# Deleting must drop the dedup entry. is_already_downloaded alone cannot show
# this — it re-checks os.path.exists and returns None either way — so assert on
# the state file, which is where a stale entry would actually accumulate.
bravo = track(1, *[s for s in SONGS if s[0] == "Bravo"][0])
check("a pruned track is no longer reported as downloaded",
      o.is_already_downloaded(bravo) is None)
state_text = pathlib.Path(root, ".antra_state.json").read_text(encoding="utf-8")
check("the pruned file is gone from the state file",
      "02 - Bravo - A2.flac" not in state_text)
check("but a surviving file is still in it", "01 - Alpha - A1.flac" in state_text)

# ── Non-audio files are never offered. ───────────────────────────────────
root = tempfile.mkdtemp(prefix="i75_naf_")
seed(root)
pathlib.Path(root, PLAYLIST, "cover.jpg").write_bytes(b"\0")
pathlib.Path(root, PLAYLIST, "notes.txt").write_bytes(b"\0")
pathlib.Path(root, PLAYLIST, ".antra-renum-0.tmp").write_bytes(b"\0")
o = organizer(root)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(SONGS)])
eq("artwork, text and staging files are not orphans",
   o.find_playlist_orphans(PLAYLIST, [p for _, p in entries]), [])

# ── Library storage mode has no playlist folder to maintain. ─────────────
#
# The folder is seeded in playlist_folder mode FIRST, then the mode is switched.
# That is the realistic case (a user changes the setting) and the only one that
# can catch the guard being dropped: with no folder on disk, find_playlist_orphans
# returns [] via the is_dir() check whether the guard is there or not, and the
# assertion passes vacuously. The negative control missed this until the seed
# was added.
root = tempfile.mkdtemp(prefix="i75_lib_")
seed(root)
check("the playlist folder exists before the mode switch",
      pathlib.Path(root, PLAYLIST).is_dir())
o = organizer(root, playlist_storage_mode="library")
# A track the seeded folder does not contain, so library mode actually files
# something and the "outside the playlist folder" claim is not vacuous.
entries = run(o, [track(1, "Delta", "A4", "sid4")])
eq("library mode reports no orphans even with a playlist folder present",
   o.find_playlist_orphans(PLAYLIST, []), [])
eq("library mode renames nothing", len(o.renumber_playlist_folder(PLAYLIST, entries)), 0)
eq("the old playlist folder is left exactly as it was", names(root),
   ["01 - Alpha - A1.flac", "02 - Bravo - A2.flac", "03 - Charlie - A3.flac"])
# Compare the parent to the playlist folder itself, not the playlist NAME: these
# fixtures use the playlist name as the album too, so <root>/A4/Liked Songs/ is a
# legitimate library path that a name check would wrongly reject.
check("and the new library-mode file is filed outside the playlist folder",
      pathlib.Path(entries[0][1]).parent.resolve() != pathlib.Path(root, PLAYLIST).resolve(),
      entries[0][1])

# ══════════════════════════════════════════════════════════════════════════
# 4. PRUNE MODE NORMALISATION — anything unknown must fail safe to "ask",
#    the only mode that cannot delete.
# ══════════════════════════════════════════════════════════════════════════
print("4. prune mode")
for value, want in [("ask", "ask"), ("auto", "auto"), ("off", "off"), ("AUTO", "auto"),
                    ("  off  ", "off"), ("", "ask"), (None, "ask"), ("delete", "ask"),
                    ("true", "ask"), (0, "ask")]:
    eq(f"normalize_prune_mode({value!r})", normalize_prune_mode(value), want)

root = tempfile.mkdtemp(prefix="i75_mode_")
eq("an unrecognised configured mode lands on ask",
   organizer(root, playlist_prune_orphans="wipe").playlist_prune_orphans, "ask")
eq("renumber defaults ON", organizer(root).playlist_renumber, True)


# ══════════════════════════════════════════════════════════════════════════
# 5. WIRING — env -> load_config -> json_cli._make_organizer.
#
#    Driven through json_cli's OWN factory, not a hand-built organizer: the
#    BUG-23 trap is that a setting reaches the dataclass but never reaches the
#    object that uses it, and only the real chain can show that.
# ══════════════════════════════════════════════════════════════════════════
print("5. wiring")

WIRE = r'''
import os, sys, json
sys.path.insert(0, %(root)r)
os.environ["OUTPUT_DIR"] = %(lib)r
for k, v in json.loads(%(env)r).items():
    os.environ[k] = v
import antra.json_cli as jc
from antra.core.config import load_config
c = load_config()
o = jc._make_organizer(c)
print(json.dumps({"renumber": o.playlist_renumber, "prune": o.playlist_prune_orphans}))
'''


def wiring(env: dict) -> dict:
    lib = tempfile.mkdtemp(prefix="i75_wire_")
    src = WIRE % {"root": str(pathlib.Path(__file__).resolve().parents[2]),
                  "lib": lib, "env": __import__("json").dumps(env)}
    out = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True,
                         cwd=str(pathlib.Path(__file__).resolve().parents[2]))
    last = [l for l in out.stdout.splitlines() if l.startswith("{")]
    if not last:
        return {"error": out.stderr[-400:]}
    return __import__("json").loads(last[-1])

eq("defaults with nothing set", wiring({}), {"renumber": True, "prune": "ask"})
eq("PLAYLIST_RENUMBER=false reaches the organizer",
   wiring({"PLAYLIST_RENUMBER": "false"}), {"renumber": False, "prune": "ask"})
eq("PLAYLIST_PRUNE_ORPHANS=auto reaches the organizer",
   wiring({"PLAYLIST_PRUNE_ORPHANS": "auto"}), {"renumber": True, "prune": "auto"})
eq("a junk prune mode still fails safe through the real chain",
   wiring({"PLAYLIST_PRUNE_ORPHANS": "wipe"}), {"renumber": True, "prune": "ask"})

# The BUG-23 guard itself: json_cli must set BOTH vars unconditionally, so a
# stale .env cannot decide the behaviour when the key is absent from config.json.
cli_src = io.open(pathlib.Path(__file__).resolve().parents[1] / "json_cli.py",
                  encoding="utf-8", newline="").read()
for var in ("PLAYLIST_RENUMBER", "PLAYLIST_PRUNE_ORPHANS"):
    check(f'json_cli sets {var} unconditionally',
          f'os.environ["{var}"]' in cli_src)
check("the renumber default is not written with `or`, which cannot express False",
      'settings.get("playlist_renumber") or' not in cli_src)


# ══════════════════════════════════════════════════════════════════════════
# 6. ENGINE ORCHESTRATION — the two helpers download_playlist calls, driven on
#    a real DownloadEngine instance so the complete_playlist gate and the
#    prune-before-renumber order are tested where they actually live.
# ══════════════════════════════════════════════════════════════════════════
print("6. engine")
from antra.core.engine import DownloadEngine  # noqa: E402
from antra.core.events import EngineEventType  # noqa: E402
from antra.core.models import DownloadResult, DownloadStatus  # noqa: E402


class Engine:
    """DownloadEngine with __init__ bypassed — only the two helpers are wanted,
    and a real one needs a resolver, a config and a network."""

    def __new__(cls, org, mode="ask"):
        e = object.__new__(DownloadEngine)
        e.organizer = org
        org.playlist_prune_orphans = normalize_prune_mode(mode)
        e.events = []
        e.event_callback = e.events.append
        e._emit_lock = __import__("threading").Lock()
        return e


def results_for(entries):
    return [DownloadResult(track=t, status=DownloadStatus.COMPLETED, file_path=p)
            for t, p in entries]


# ── The gate. A partial run must touch nothing. ──────────────────────────
root = tempfile.mkdtemp(prefix="i75_eng_")
seed(root)
o = organizer(root)
entries = run(o, [track(i + 1, *s) for i, s in enumerate(shifted(SONGS))])
res = results_for(entries)
before = names(root)

# The partial run covers ONE track of four, which is the destructive shape: the
# other three are in the folder and are not in `res`, so without the gate they
# would every one be reported as a leftover to delete. Passing the full list
# here would make the sabotage undetectable — the negative control said so.
partial = [r for r in res if r.track.title == "Alpha"]
e = Engine(o)
e._handle_playlist_orphans(PLAYLIST, partial, False)
e._renumber_playlist(PLAYLIST, partial, False)
eq("a partial run renames nothing", names(root), before)
eq("a partial run reports no orphans", e.events, [])

# ...and the control that must come out the other way: the SAME partial list
# with the gate open really would offer the other three files.
e2 = Engine(organizer(root))
e2._handle_playlist_orphans(PLAYLIST, partial, True)
eq("with the gate open, the uncovered tracks ARE reported",
   sorted(os.path.basename(p) for p in e2.events[0].paths),
   ["01 - NewSong - A0.flac", "02 - Bravo - A2.flac", "03 - Charlie - A3.flac"])
check("but nothing was deleted in ask mode", names(root) == before, names(root))

# ── And the control: the same call with complete_playlist=True does act. ──
e._renumber_playlist(PLAYLIST, res, True)
eq("a complete run renumbers", names(root),
   ["01 - NewSong - A0.flac", "02 - Alpha - A1.flac",
    "03 - Bravo - A2.flac", "04 - Charlie - A3.flac"])
eq("and the results were repointed in place",
   sorted(os.path.basename(r.file_path) for r in res), names(root))

# ── "ask" reports without deleting; "auto" deletes. ──────────────────────
root = tempfile.mkdtemp(prefix="i75_eng2_")
seed(root)
kept = [s for s in SONGS if s[0] != "Bravo"]
o = organizer(root)
res = results_for(run(o, [track(i + 1, *s) for i, s in enumerate(kept)]))
e = Engine(o, "ask")
e._handle_playlist_orphans(PLAYLIST, res, True)
eq("ask emits one orphan event", [ev.type for ev in e.events],
   [EngineEventType.PLAYLIST_ORPHANS])
eq("naming the playlist", e.events[0].message, PLAYLIST)
check("with pruned=False", e.events[0].pruned is False)
check("and the file still on disk", os.path.exists(e.events[0].paths[0]))

root = tempfile.mkdtemp(prefix="i75_eng3_")
seed(root)
o = organizer(root)
res = results_for(run(o, [track(i + 1, *s) for i, s in enumerate(kept)]))
e = Engine(o, "auto")
e._handle_playlist_orphans(PLAYLIST, res, True)
check("auto reports pruned=True", e.events[0].pruned is True)
check("and the file is gone", not os.path.exists(e.events[0].paths[0]))
eq("leaving only the playlist's own tracks", names(root),
   ["01 - Alpha - A1.flac", "03 - Charlie - A3.flac"])

root = tempfile.mkdtemp(prefix="i75_eng4_")
seed(root)
o = organizer(root)
res = results_for(run(o, [track(i + 1, *s) for i, s in enumerate(kept)]))
e = Engine(o, "off")
e._handle_playlist_orphans(PLAYLIST, res, True)
eq("off emits nothing", e.events, [])
check("and deletes nothing", os.path.exists(pathlib.Path(root, PLAYLIST, "02 - Bravo - A2.flac")))

# ── Prune BEFORE renumber: an orphan holding a wanted name would otherwise
#    block the rename. Uses the number-only template, where the orphan's stem
#    really does collide with a destination.
root = tempfile.mkdtemp(prefix="i75_ord_")
seed(root, **NUMBERED)
o = organizer(root, **NUMBERED)
res = results_for(run(o, [track(i + 1, *s) for i, s in enumerate(kept)]))
e = Engine(o, "auto")
e._handle_playlist_orphans(PLAYLIST, res, True)
e._renumber_playlist(PLAYLIST, res, True)
eq("pruning first lets the renumber close the gap", names(root), ["01.flac", "02.flac"])
eq("and the survivors are the playlist's own, in order",
   [FLAC(str(pathlib.Path(root, PLAYLIST, n)))["title"][0] for n in names(root)],
   ["Alpha", "Charlie"])

# The other order leaves the hole, which is what the comment in engine.py claims.
root = tempfile.mkdtemp(prefix="i75_ord2_")
seed(root, **NUMBERED)
o = organizer(root, **NUMBERED)
res = results_for(run(o, [track(i + 1, *s) for i, s in enumerate(kept)]))
e = Engine(o, "auto")
e._renumber_playlist(PLAYLIST, res, True)      # deliberately the WRONG order
eq("renumbering first is blocked by the orphan still holding 02",
   names(root), ["01.flac", "02.flac", "03.flac"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
