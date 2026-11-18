"""Issue #78 - playlist downloads ignored the configured Folder Structure.

A playlist track was ALWAYS filed under <root>/<Playlist Name>/, whatever the
folder/filename templates said, so users had to move files by hand afterwards.
`playlist_storage_mode` (contributed as PR #83 by @seguinGregory, adapted here)
makes that a choice:

    "playlist_folder"  <- default, byte-identical to the old behaviour
    "library"          <- file the track exactly like an album download

The default is deliberately unchanged, so the first block below is a
backward-compatibility pin, not a new feature test.

Run:  python antra/utils/test_playlist_storage.py
Negative control: antra/utils/test_playlist_storage_negctl.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata
from antra.utils.organizer import LibraryOrganizer

FAILURES: list[str] = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n      got:  {got!r}\n      want: {want!r}")


def truth(label, cond, detail=""):
    if not cond:
        FAILURES.append(f"{label}{('  ' + detail) if detail else ''}")


def plt(title, artist, album, *, year, track_number, playlist="My Playlist",
        position=None, disc_number=1, isrc=None):
    return TrackMetadata(
        title=title, artists=[artist], album=album, album_artists=[artist],
        request_kind="playlist", playlist_name=playlist,
        playlist_position=position if position is not None else track_number,
        release_year=year, track_number=track_number, disc_number=disc_number,
        isrc=isrc,
    )


def organizer(**kw):
    root = tempfile.mkdtemp(prefix="pls")
    # The index is disabled so these assertions are about PATHS only; dedup is
    # exercised separately below with it switched on.
    kw.setdefault("library_index_enabled", False)
    return LibraryOrganizer(root, **kw), Path(root)


TPL = dict(folder_structure_template="{album_artist}/{year} - {album}",
           album_track_filename_template="{track} - {title}")


# ── 1. default mode is unchanged ────────────────────────────────────────────

org, root = organizer(**TPL)
check("default mode is playlist_folder", org.playlist_storage_mode, "playlist_folder")
check("default mode is not library mode", org.playlist_in_library_mode, False)
out = Path(org.get_output_path(plt("Track 1", "Artist A", "Album A", year=2020, track_number=1)))
check("default: parent is the playlist folder", out.parent, root / "My Playlist")
truth("default: playlist numbering is used", out.name.startswith("01 "), out.name)

# An explicitly-set default, and an unrecognised value, must both stay safe.
for value, label in (("playlist_folder", "explicit default"), ("", "empty string"),
                     ("nonsense", "unrecognised value")):
    o, r = organizer(playlist_storage_mode=value, **TPL)
    p = Path(o.get_output_path(plt("T", "A", "Alb", year=2020, track_number=1)))
    check(f"{label} still uses the playlist folder", p.parent, r / "My Playlist")


# ── 2. library mode files playlist tracks like album tracks ─────────────────

org, root = organizer(playlist_storage_mode="library", **TPL)
check("library mode reads back", org.playlist_in_library_mode, True)

tracks = [
    plt("Track 1", "Artist A", "Album A", year=2020, track_number=1, position=1),
    plt("Track 2", "Artist B", "Album B", year=2022, track_number=5, position=2),
    plt("Track 3", "Artist A", "Album C", year=2019, track_number=9, position=3),
]
outs = [Path(org.get_output_path(t)) for t in tracks]
check("library: each track lands in its own artist/album folder", outs, [
    root / "Artist A" / "2020 - Album A" / "01 - Track 1",
    root / "Artist B" / "2022 - Album B" / "05 - Track 2",
    root / "Artist A" / "2019 - Album C" / "09 - Track 3",
])
truth("library: the playlist name appears nowhere in any path",
      all("My Playlist" not in o.parts for o in outs))
# The numbering must be the ALBUM track number, not the playlist position -
# otherwise two playlists produce two differently-named copies of one track.
truth("library: album track number wins over playlist position",
      outs[1].name.startswith("05 "), outs[1].name)


# ── 3. library mode inherits the rest of the album pipeline ─────────────────

org, root = organizer(playlist_storage_mode="library", multi_disc_handling="folder", **TPL)
out = Path(org.get_output_path(
    plt("Disc Two", "Artist A", "Album A", year=2020, track_number=3, disc_number=2)))
check("library: multi-disc folder handling applies", out.parent,
      root / "Artist A" / "2020 - Album A" / "CD2")


# ── 4. the playlist copy and the manifest ───────────────────────────────────

org, root = organizer(playlist_storage_mode="library", **TPL)
track = plt("Track 1", "Artist A", "Album A", year=2020, track_number=1)
canonical = org.get_output_path(track) + ".flac"
Path(canonical).parent.mkdir(parents=True, exist_ok=True)
Path(canonical).write_bytes(b"audio")
copied = org.ensure_playlist_copy(track, canonical)
check("library: ensure_playlist_copy is a no-op", Path(copied), Path(canonical))
truth("library: no second copy under a playlist folder",
      not (root / "My Playlist").exists())

manifest = Path(org.write_playlist_manifest("My Playlist", [canonical]))
check("library: the .m3u is still written at the library root", manifest, root / "My Playlist.m3u")
lines = manifest.read_text(encoding="utf-8").splitlines()
check("library: manifest header", lines[0], "#EXTM3U")
check("library: manifest points into the library",
      lines[1], "Artist A/2020 - Album A/01 - Track 1.flac")

# Control: the SAME call in the default mode really does make a second copy,
# so the no-op above is a property of library mode and not of this fixture.
org2, root2 = organizer(**TPL)
canonical2 = str(root2 / "Artist A" / "2020 - Album A" / "01 - Track 1.flac")
Path(canonical2).parent.mkdir(parents=True, exist_ok=True)
Path(canonical2).write_bytes(b"audio")
copied2 = Path(org2.ensure_playlist_copy(track, canonical2))
truth("CONTROL default mode: a playlist copy IS made", copied2 != Path(canonical2), str(copied2))
check("CONTROL default mode: the copy is in the playlist folder",
      copied2.parent, root2 / "My Playlist")


# ── 5. the issue's second half: the same track on two playlists ─────────────

org, root = organizer(playlist_storage_mode="library", **TPL)
a = plt("Shared", "Artist A", "Album A", year=2020, track_number=4,
        playlist="Playlist A", position=1, isrc="GBUM71029604")
b = plt("Shared", "Artist A", "Album A", year=2020, track_number=4,
        playlist="Playlist B", position=12, isrc="GBUM71029604")
check("library: one track on two playlists resolves to ONE path",
      Path(org.get_output_path(a)), Path(org.get_output_path(b)))

org2, root2 = organizer(**TPL)
truth("CONTROL default mode: two playlists give two different paths",
      Path(org2.get_output_path(a)) != Path(org2.get_output_path(b)))


# ── report ──────────────────────────────────────────────────────────────────

TOTAL = 19
if FAILURES:
    print(f"FAIL - {len(FAILURES)} of {TOTAL} checks failed:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print(f"PASS - {TOTAL} checks (default unchanged, library mode, manifest, dedup)")
