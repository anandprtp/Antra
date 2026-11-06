#!/usr/bin/env python3
"""Unit tests for state-only smart-dedup index and batched state flush."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from antra.core.models import TrackMetadata
from antra.utils.organizer import STATE_FILE, TRACK_KEY_PREFIX, LibraryOrganizer


def _track(**kwargs) -> TrackMetadata:
    defaults = {
        "title": "Test Song",
        "artists": ["Test Artist"],
        "album": "Test Album",
        "track_number": 1,
        "isrc": "USABC1234567",
        "spotify_id": "spotifytrackid01",
    }
    defaults.update(kwargs)
    return TrackMetadata(**defaults)


def _write_audio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-audio")


class OrganizerIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _organizer(self, **kwargs) -> LibraryOrganizer:
        opts = {
            "root": str(self.root),
            "full_albums": False,
            "filename_conflict_behavior": "skip",
        }
        opts.update(kwargs)
        return LibraryOrganizer(**opts)

    def test_state_seed_skips_without_tag_scan(self) -> None:
        audio = self.root / "Other Artist" / "Other Album" / "01 - Something.flac"
        _write_audio(audio)
        state = {
            f"{TRACK_KEY_PREFIX}isrc:usabc1234567": str(audio.resolve()),
        }
        (self.root / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

        with patch.object(
            LibraryOrganizer,
            "_extract_identity_keys_from_file",
            side_effect=AssertionError("init must not tag-scan the library"),
        ):
            organizer = self._organizer()

        found = organizer.is_already_downloaded(_track())
        self.assertEqual(found, str(audio.resolve()))

    def test_external_file_not_in_identity_index_until_rebuild(self) -> None:
        # File lives under a path that will NOT match the track's canonical
        # output path, so only the identity index could skip it.
        external = self.root / "External" / "Drop" / "song.flac"
        _write_audio(external)
        keys = ["isrc:usabc1234567", "title_artist:test song:test artist"]
        missing_base = str(self.root / "nowhere" / "missing")

        organizer = self._organizer()
        with patch.object(organizer, "get_output_path", return_value=missing_base):
            self.assertIsNone(organizer.is_already_downloaded(_track()))

        with patch.object(
            LibraryOrganizer,
            "_extract_identity_keys_from_file",
            return_value=keys,
        ):
            report = organizer.rebuild_identity_index(persist=True)

        self.assertEqual(report["files_scanned"], 1)
        self.assertGreaterEqual(report["keys_written"], 1)
        self.assertTrue((self.root / STATE_FILE).exists())

        fresh = self._organizer()
        with patch.object(fresh, "get_output_path", return_value=missing_base):
            found = fresh.is_already_downloaded(_track())
        self.assertEqual(found, str(external.resolve()))

    def test_mark_downloaded_batches_until_flush(self) -> None:
        organizer = self._organizer()
        audio_a = self.root / "A" / "Album" / "01 - One.flac"
        audio_b = self.root / "B" / "Album" / "01 - Two.flac"
        _write_audio(audio_a)
        _write_audio(audio_b)

        organizer.mark_downloaded(_track(title="One", isrc="USAAA1111111", spotify_id="a"), str(audio_a))
        organizer.mark_downloaded(_track(title="Two", isrc="USBBB2222222", spotify_id="b"), str(audio_b))
        self.assertFalse((self.root / STATE_FILE).exists())
        self.assertTrue(organizer._state_dirty)

        organizer.flush_state()
        self.assertTrue((self.root / STATE_FILE).exists())
        self.assertFalse(organizer._state_dirty)

        payload = json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        self.assertIn(f"{TRACK_KEY_PREFIX}isrc:usaaa1111111", payload)
        self.assertIn(f"{TRACK_KEY_PREFIX}isrc:usbbb2222222", payload)

    def test_init_does_not_rglob_tag_scan(self) -> None:
        audio = self.root / "Artist" / "Album" / "01 - Song.flac"
        _write_audio(audio)

        with patch.object(Path, "rglob", side_effect=AssertionError("rglob must not run on init")) as mocked:
            # Path.rglob is used by rebuild; init must not call it.
            # Also guard extract so a future regression fails loudly.
            with patch.object(
                LibraryOrganizer,
                "_extract_identity_keys_from_file",
                side_effect=AssertionError("tag extract must not run on init"),
            ):
                organizer = self._organizer()
            mocked.assert_not_called()
        self.assertEqual(organizer._identity_index, {})


if __name__ == "__main__":
    unittest.main()
