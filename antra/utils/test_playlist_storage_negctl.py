"""Negative control for issue #78 - prove test_playlist_storage.py can fail.

Each sabotage re-creates a way the feature could be silently dead. Every one
must be CAUGHT, or the main suite is passing for free.

Run:  python antra/utils/test_playlist_storage_negctl.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata
from antra.utils.organizer import LibraryOrganizer

CAUGHT: list[str] = []
MISSED: list[str] = []

TPL = dict(folder_structure_template="{album_artist}/{year} - {album}",
           album_track_filename_template="{track} - {title}",
           library_index_enabled=False)


def sabotage(label, fn):
    try:
        detected = fn()
    except Exception as exc:
        detected = True
        label = f"{label} (raised {type(exc).__name__})"
    (CAUGHT if detected else MISSED).append(label)


def track(playlist="My Playlist", position=1):
    return TrackMetadata(
        title="Track 1", artists=["Artist A"], album="Album A",
        album_artists=["Artist A"], request_kind="playlist",
        playlist_name=playlist, playlist_position=position,
        release_year=2020, track_number=1,
    )


def org(mode):
    root = tempfile.mkdtemp(prefix="neg")
    return LibraryOrganizer(root, playlist_storage_mode=mode, **TPL), Path(root)


# 1. The pre-fix organizer: the playlist branch is unconditional, so library
#    mode does nothing at all. This IS the reported bug.
def sab_branch_always_taken():
    original = LibraryOrganizer.playlist_in_library_mode
    try:
        LibraryOrganizer.playlist_in_library_mode = property(lambda self: False)
        o, root = org("library")
        return Path(o.get_output_path(track())).parent == root / "My Playlist"
    finally:
        LibraryOrganizer.playlist_in_library_mode = original


# 2. The mode never reaches the organizer (a caller that forgot the kwarg -
#    exactly how the four duplicated LibraryOrganizer constructions used to
#    drift apart before _make_organizer consolidated them).
def sab_kwarg_dropped():
    root = tempfile.mkdtemp(prefix="neg")
    o = LibraryOrganizer(root, **TPL)          # no playlist_storage_mode at all
    return Path(o.get_output_path(track())).parent == Path(root) / "My Playlist"


# 3. The branch inverted: the DEFAULT would start filing into the library,
#    which would silently reorganise every existing user's playlists.
def sab_default_becomes_library():
    original = LibraryOrganizer.playlist_in_library_mode
    try:
        LibraryOrganizer.playlist_in_library_mode = property(lambda self: True)
        o, root = org("playlist_folder")
        return Path(o.get_output_path(track())).parent != root / "My Playlist"
    finally:
        LibraryOrganizer.playlist_in_library_mode = original


# 4. The mode predicate written as "anything that is not the default" instead
#    of "exactly library". A typo'd or future value then silently reorganises
#    the user's playlists into the library.
def sab_predicate_is_not_default():
    original = LibraryOrganizer.playlist_in_library_mode
    try:
        LibraryOrganizer.playlist_in_library_mode = property(
            lambda self: self.playlist_storage_mode != "playlist_folder")
        o, root = org("nonsense")
        return Path(o.get_output_path(track())).parent != root / "My Playlist"
    finally:
        LibraryOrganizer.playlist_in_library_mode = original


# 5. Same sabotage as (1) but measured on DISK rather than on a returned path:
#    with the branch forced back on, library mode writes a second physical copy
#    under the playlist folder - two files per track, which is what the "no-op"
#    claim in the main suite is actually asserting does not happen.
def sab_playlist_copy_still_made():
    original = LibraryOrganizer.playlist_in_library_mode
    try:
        o, root = org("library")
        t = track()
        canonical = o.get_output_path(t) + ".flac"     # library path, pre-sabotage
        Path(canonical).parent.mkdir(parents=True, exist_ok=True)
        Path(canonical).write_bytes(b"audio")
        LibraryOrganizer.playlist_in_library_mode = property(lambda self: False)
        copied = Path(o.ensure_playlist_copy(t, canonical))
        return copied != Path(canonical) and (root / "My Playlist").exists()
    finally:
        LibraryOrganizer.playlist_in_library_mode = original


sabotage("playlist branch always taken (the reported bug)", sab_branch_always_taken)
sabotage("playlist_storage_mode kwarg dropped by the caller", sab_kwarg_dropped)
sabotage("default silently becomes library mode", sab_default_becomes_library)
sabotage("mode predicate is \"not default\" instead of \"is library\"", sab_predicate_is_not_default)
sabotage("library mode still writes a second playlist copy", sab_playlist_copy_still_made)

print(f"sabotages caught: {len(CAUGHT)}/{len(CAUGHT) + len(MISSED)}")
for c in CAUGHT:
    print("  caught  -", c)
for m in MISSED:
    print("  MISSED  -", m)
if MISSED:
    print("FAIL - a sabotage went undetected")
    sys.exit(1)
print("PASS - every sabotage is detected")
