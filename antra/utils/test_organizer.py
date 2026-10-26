from pathlib import Path

from antra.core.models import TrackMetadata
from antra.utils.organizer import LibraryOrganizer


def make_playlist_track(title, artist, album, *, year, track_number):
    return TrackMetadata(
        title=title,
        artists=[artist],
        album=album,
        request_kind="playlist",
        playlist_name="My Playlist",
        playlist_position=track_number,
        album_artists=[artist],
        release_year=year,
        track_number=track_number,
    )


def test_playlist_folder_mode_preserves_playlist_destination(tmp_path):
    organizer = LibraryOrganizer(str(tmp_path))
    track = make_playlist_track("Track 1", "Artist A", "Album A", year=2020, track_number=1)

    output = Path(organizer.get_output_path(track))

    assert output.parent == tmp_path / "My Playlist"
    assert output.name == "01 - Track 1"


def test_library_mode_uses_configured_structure_for_each_playlist_track(tmp_path):
    organizer = LibraryOrganizer(
        str(tmp_path),
        playlist_storage_mode="library",
        folder_structure_template="{album_artist}/{year} - {album}",
        album_track_filename_template="{track} - {title}",
    )
    tracks = [
        make_playlist_track("Track 1", "Artist A", "Album A", year=2020, track_number=1),
        make_playlist_track("Track 2", "Artist B", "Album B", year=2022, track_number=2),
        make_playlist_track("Track 3", "Artist A", "Album C", year=2019, track_number=3),
    ]

    outputs = [Path(organizer.get_output_path(track)) for track in tracks]

    assert outputs == [
        tmp_path / "Artist A" / "2020 - Album A" / "01 - Track 1",
        tmp_path / "Artist B" / "2022 - Album B" / "02 - Track 2",
        tmp_path / "Artist A" / "2019 - Album C" / "03 - Track 3",
    ]
    assert all("My Playlist" not in output.parts for output in outputs)