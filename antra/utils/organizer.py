"""
Handles library layout, global deduplication, and playlist manifest generation.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TSRC, TXXX
from mutagen.mp4 import MP4

from antra.core.models import TrackMetadata
from antra.utils.longpath import extended_path
from antra.utils.library_index import compute_logic_version, get_library_index
from datetime import date as _date

from antra_shared.filename_prefs import (
    RUN_DATE_KEY,
    transform_text,
    build_folder_path,
    build_single_track_stem,
    build_track_stem,
    disc_folder_name,
    is_disc_folder_name,
    migrate_legacy_templates,
)

logger = logging.getLogger(__name__)

STATE_FILE = ".antra_state.json"
_STATE_WRITE_LOCK = threading.Lock()   # shared: every worker's _save_state goes through it
TRACK_KEY_PREFIX = "TRACK:"
FAILED_PREFIX = "FAILED:"
SUPPORTED_AUDIO_EXTENSIONS = (".flac", ".mp3", ".aac", ".m4a", ".mp4", ".opus")

# issue #75 — what to do with files left in a playlist folder that the playlist
# no longer contains. "ask" only REPORTS them; the deletion is a separate,
# explicit user action. Anything unrecognised degrades to "ask", which is the
# only mode that cannot delete a file.
PRUNE_MODES = ("ask", "auto", "off")

# Name stem used by the two-phase renumber. A staged file ends in ".tmp", which
# is deliberately NOT a supported audio extension: a crash mid-rename then
# leaves files the library scan and the orphan finder both ignore, rather than
# half-named audio they would index or offer to delete.
_RENUMBER_TMP_SUFFIX = ".antra-renum"


def normalize_prune_mode(value) -> str:
    """Coerce a configured prune mode to one of PRUNE_MODES, defaulting to "ask"."""
    text = str(value or "").strip().lower()
    return text if text in PRUNE_MODES else "ask"


# v2.0.0 SF-4 — the cache is only safe while the key-derivation logic is
# unchanged, so its version is a hash of these functions' source. Editing any of
# them orphans every cached row automatically; forgetting to bump a constant by
# hand is exactly the failure mode that would serve stale dedup keys.
_IDENTITY_LOGIC_FUNCTIONS: tuple = ()

_PRUNED_ROOTS_ONCE = False


def _prune_missing_roots_once(index) -> None:
    """Drop rows for libraries that no longer exist — at most once per process."""
    global _PRUNED_ROOTS_ONCE
    if _PRUNED_ROOTS_ONCE:
        return
    _PRUNED_ROOTS_ONCE = True
    try:
        index.prune_missing_roots()
    except Exception:  # pragma: no cover - defensive
        pass


class LibraryOrganizer:
    """
    Library structure (no Albums/ or Playlists/ wrappers):
      <root>/<Artist>/<Album (Year)>/<NN - Track Title>.<ext>   (standard mode)
      <root>/<Album (Year)>/<NN - Track Title>.<ext>            (flat mode)
      <root>/<Playlist Name>/<NN - Track Title>.<ext>   (playlist_storage_mode="playlist_folder")
      <root>/<Playlist Name>.m3u

    With playlist_storage_mode="library" (issue #78) a playlist track is filed
    exactly like an album track - the playlist name only survives in the .m3u,
    which then points into the library. Nothing else changes: the manifest is
    still written, and ensure_playlist_copy becomes a no-op by construction
    because the playlist path and the canonical path are the same path.

    Deduplication is global across the library. The first downloaded file path
    becomes canonical and later playlist/album/song downloads reuse that file.
    """

    def __init__(
        self,
        root: str,
        full_albums: bool = False,
        library_index_enabled: bool = True,
        library_index_db_path: str = "",
        folder_structure: str = "standard",
        album_folder_structure: str = "",
        playlist_folder_structure: str = "",
        # issue #78 - "playlist_folder" (default, unchanged) | "library"
        playlist_storage_mode: str = "playlist_folder",
        # issue #75 - rename playlist files when their position moved.
        playlist_renumber: bool = True,
        # issue #75 - "ask" (default, report only) | "auto" | "off"
        playlist_prune_orphans: str = "ask",
        single_track_structure: str = "album_numbered",
        filename_format: str = "default",
        single_track_filename_template: str = "",
        album_track_filename_template: str = "",
        folder_structure_template: str = "",
        flat_library_root: bool = False,
        multi_disc_handling: str = "prefix",
        track_number_padding: int = 2,
        illegal_character_replacement: str = "",
        whitespace_handling: str = "preserve",
        filename_conflict_behavior: str = "skip",
        # v2.0.0 DMX-2 — output-only text transforms. They reach the tagger for
        # free: DownloadEngine hands it organizer.filename_preferences, which is
        # the migrated dict built below.
        title_casing: str = "nothing",
        artist_casing: str = "nothing",
        remove_album_version: bool = False,
        remove_duplicate_artists: bool = True,
        featured_artists_mode: str = "nothing",
        multi_artist_separator: str = "default",
        album_various_artists: bool = True,
    ):
        self.root = Path(root).resolve()
        self.full_albums = full_albums
        # v1.1.8 FEAT-11 — clearing the folder template means "no album folder";
        # every track lands directly in the library root.
        self.flat_library_root = bool(flat_library_root)
        legacy_structure = folder_structure or "standard"
        self.folder_structure = legacy_structure
        self.album_folder_structure = album_folder_structure or legacy_structure
        self.playlist_folder_structure = playlist_folder_structure or legacy_structure
        self.playlist_storage_mode = (playlist_storage_mode or "playlist_folder").strip().lower()
        self.playlist_renumber = bool(playlist_renumber)
        self.playlist_prune_orphans = normalize_prune_mode(playlist_prune_orphans)
        self.single_track_structure = single_track_structure or "album_numbered"
        self.filename_format = filename_format
        self.filename_preferences = migrate_legacy_templates(
            {
                "single_track_filename_template": single_track_filename_template,
                "album_track_filename_template": album_track_filename_template,
                "folder_structure_template": folder_structure_template,
                "multi_disc_handling": multi_disc_handling,
                "track_number_padding": track_number_padding,
                "illegal_character_replacement": illegal_character_replacement,
                "whitespace_handling": whitespace_handling,
                "filename_conflict_behavior": filename_conflict_behavior,
                "title_casing": title_casing,
                "artist_casing": artist_casing,
                "remove_album_version": remove_album_version,
                "remove_duplicate_artists": remove_duplicate_artists,
                "featured_artists_mode": featured_artists_mode,
                "multi_artist_separator": multi_artist_separator,
                "album_various_artists": album_various_artists,
            },
            filename_format=filename_format,
            album_folder_structure=self.album_folder_structure,
        )
        # v2.0.0 DMX-3 — pin {today} once per organizer (i.e. per run) so an
        # album that downloads across midnight cannot split into two folders.
        self.filename_preferences[RUN_DATE_KEY] = _date.today().isoformat()
        self.root.mkdir(parents=True, exist_ok=True)
        # No Albums/ or Playlists/ wrappers — everything lives directly under root.
        self.albums_root = self.root
        self.playlists_root = self.root
        self._state_path = self.root / STATE_FILE
        self._state = self._load_state()
        self._identity_index: dict[str, str] = {}
        # An album can carry two DIFFERENT recordings under the same
        # title+artist (a studio track plus its live / acoustic re-cut).
        # Those collide on the fuzzy title_artist key and the second was
        # skipped as 'already downloaded' (issue: Anne Wilson - Stars).
        # ISRC is the only field that can say *definitely a different
        # recording*, so it is remembered per file and vetoes a fuzzy hit.
        # Resolved+normcased path -> upper-case ISRC.
        self._path_isrc: dict[str, str] = {}
        # issue #75 — identity map of ONE playlist folder, built lazily and only
        # when the cheap check in ensure_playlist_copy cannot answer. Keyed by
        # the resolved folder path. Rebuilt never: the copies this run creates
        # are folded in as they are made.
        self._playlist_folder_index: dict[str, dict[str, str]] = {}
        # v2.0.0 SF-4 — persistent identity-key cache. Resolved BEFORE the scan
        # because _build_identity_index() is what consumes it. A None index is
        # the pre-SF-4 behaviour exactly: every file is read.
        self._index_root = str(self.root.resolve())
        self._library_index = None
        self._index_version = ""
        if library_index_enabled and not full_albums:
            self._library_index = get_library_index(library_index_db_path or None)
            if self._library_index is not None:
                self._index_version = compute_logic_version(_IDENTITY_LOGIC_FUNCTIONS)
                _prune_missing_roots_once(self._library_index)
        if not full_albums:
            self._build_identity_index()

    # ── Public API ────────────────────────────────────────────────────────

    def get_output_path(self, track: TrackMetadata) -> str:
        """Return the target output path WITHOUT extension."""
        if track.playlist_name and not self.playlist_in_library_mode:
            folder = self.playlist_folder_for(track.playlist_name)
            filename = self.playlist_stem_for(track)
            folder.mkdir(parents=True, exist_ok=True)
            return str(self._resolve_conflict_path(folder, filename, track=track))

        if (track.request_kind or "").lower() == "track":
            return self._single_track_output_path(track)

        folder = self._with_disc_folder(self._album_folder(track), track)
        filename = self._format_filename(track, track.track_number)
        folder.mkdir(parents=True, exist_ok=True)
        return str(self._resolve_conflict_path(folder, filename, track=track))

    @property
    def playlist_in_library_mode(self) -> bool:
        """True when playlist tracks are filed into the library (issue #78).

        Read as a property rather than comparing the string at each call site so
        a future third mode cannot silently take the playlist-folder branch.
        """
        return self.playlist_storage_mode == "library"

    def playlist_folder_for(self, playlist_name: str) -> Path:
        """The folder a playlist's files live in, in playlist_folder mode.

        Split out of get_output_path (issue #75) because renumbering, orphan
        detection and the "is this file already the playlist copy?" check all
        have to agree with it exactly. Deriving the folder twice is how they
        would silently drift apart. It does NOT create the folder — the callers
        that write into it do that.
        """
        playlist_dir = self._safe(playlist_name)
        if self.playlist_folder_structure == "flat":
            return self.root / playlist_dir
        return self.playlists_root / playlist_dir

    def playlist_stem_for(self, track: TrackMetadata) -> str:
        """The filename (no extension) this track should carry RIGHT NOW.

        The number is the playlist position, which is exactly why issue #75
        exists: Spotify's Liked Songs prepends, so every position shifts and the
        stem of an already-downloaded file stops matching.
        """
        track_number = track.playlist_position or track.track_number
        return self._format_filename(track, track_number, disc_number=1)

    def _with_disc_folder(self, album_folder: Path, track: TrackMetadata) -> Path:
        """Append CD<n>/ in multi_disc_handling=folder mode (v2.0.0 DMX-4).

        Skipped when the "album folder" IS the library root (cleared template):
        a CD1/ at the root would merge disc 1 of every album into one folder.
        """
        if self.flat_library_root:
            return album_folder
        disc_dir = disc_folder_name(track, self.filename_preferences)
        return album_folder / disc_dir if disc_dir else album_folder

    def release_folder_for(self, file_path: str) -> Optional[str]:
        """The album folder a track belongs to — its parent, minus a CD<n>/ level.

        Only strips a disc level this organizer could have created, so an album
        that happens to be named "CD1" is never mistaken for a disc folder in
        any other mode.
        """
        try:
            parent = Path(file_path).parent
        except Exception:
            return None
        if (
            str(self.filename_preferences.get("multi_disc_handling") or "").lower() == "folder"
            and is_disc_folder_name(parent.name)
            and parent.parent != parent
        ):
            parent = parent.parent
        return str(parent)

    def artist_folder_for(self, file_path: str) -> Optional[str]:
        """The artist folder a track sits under, or None when there is not one.

        Only the standard layout has an artist level. In flat / flat-root mode,
        or for a playlist folder, the parent IS the library root - returning it
        would drop an artist image at the top of the user's library, so those
        cases return None (v2.0.0 FR-2).
        """
        try:
            release = self.release_folder_for(str(Path(file_path).resolve()))
            if not release:
                return None
            parent = Path(release).parent
        except Exception:
            return None
        if parent == self.root:
            return None
        try:
            parent.relative_to(self.root)
        except ValueError:
            return None
        return str(parent) if parent.is_dir() else None

    def _single_track_output_path(self, track: TrackMetadata) -> str:
        if self.single_track_structure == "file":
            folder = self.root
            filename = build_single_track_stem(track, self.filename_preferences)
        else:
            # Same disc level as the album path, so a single later re-fetched
            # as part of its album resolves to the file already on disk.
            folder = self._with_disc_folder(self._album_folder(track), track)
            filename = build_single_track_stem(track, self.filename_preferences)
        folder.mkdir(parents=True, exist_ok=True)
        return str(self._resolve_conflict_path(folder, filename, track=track))

    @staticmethod
    def _extract_album_from_folder_leaf(leaf: str) -> str:
        """Strip year decorations to get just the album name from a rendered folder leaf.

        Handles all year formats produced by folder_structure_template:
          "2011 - Zonoscope"   → "Zonoscope"
          "Zonoscope (2011)"   → "Zonoscope"
          "(2011) Zonoscope"   → "Zonoscope"
        """
        s = re.sub(r"^\d{4}\s*[-–—]\s*", "", leaf)
        s = re.sub(r"^\(\d{4}\)\s*", "", s)
        s = re.sub(r"\s*\(\d{4}\)$", "", s)
        s = re.sub(r"\s*[-–—]\s*\d{4}$", "", s)
        return s.strip()

    def _album_folder(self, track: TrackMetadata) -> Path:
        # Explicitly-cleared template: no album folder at all. Checked before
        # build_folder_path, which would otherwise substitute the default.
        if self.flat_library_root:
            return self.root
        custom_path = build_folder_path(track, self.filename_preferences)
        if custom_path:
            parts = custom_path.split("/")
            target = self.root.joinpath(*parts)
            # Year-variant dedup: when the exact target doesn't exist yet, look for
            # a sibling folder whose name is the same album with a different year
            # (e.g. "2010 - Zonoscope" already exists when we try to create
            # "2011 - Zonoscope").  Different metadata sources often return slightly
            # different release years for the same album, creating split folders.
            if not target.exists() and len(parts) >= 1:
                parent = target.parent
                album_name = self._extract_album_from_folder_leaf(parts[-1])
                if album_name:
                    existing = self._find_existing_album_folder(parent, album_name)
                    if existing is not None:
                        return existing
            return target
        # Use album-level artists for the folder name so joint albums
        # (e.g. "PARTYNEXTDOOR & Drake") land in one combined folder
        # instead of splitting by per-track artist.
        if track.album_artists:
            artist_dir = self._safe(", ".join(track.album_artists))
        else:
            artist_dir = self._safe(", ".join(track.artists))
        album_part = self._safe(track.album)
        if track.release_year:
            if self.album_folder_structure == "year_prefix":
                album_dir = f"({track.release_year}) {album_part}"
            else:
                album_dir = f"{album_part} ({track.release_year})"
        else:
            album_dir = album_part

        if self.album_folder_structure == "flat":
            parent = self.root
        else:
            parent = self.albums_root / artist_dir

        # If a year-variant of this album folder already exists on disk, reuse
        # it instead of creating a new folder with a different year.  This
        # prevents duplicate folders like "Anthology 1 (1963)" / "Anthology 1
        # (1995)" / "Anthology 1 (1996)" when the same album is downloaded from
        # different sources that report different release dates.
        target = parent / album_dir
        if not target.exists() and track.release_year:
            existing = self._find_existing_album_folder(parent, album_part)
            if existing is not None:
                return existing

        return target

    def _find_existing_album_folder(self, parent: Path, album_part: str) -> Optional[Path]:
        """Return an existing year-variant folder for *album_part* under *parent*.

        Looks for directories whose name matches ``<album_part> (<year>)`` or
        exactly ``<album_part>`` (no year).  Returns the first match found, or
        ``None`` if the parent directory doesn't exist yet.
        """
        if not parent.exists():
            return None
        import re as _re
        escaped = _re.escape(album_part)
        # Match both "Album (Year)" / "Album" and "(Year) Album" naming styles
        # so that switching between standard and year_prefix modes doesn't
        # create duplicate folders for the same album.
        pat_suffix = _re.compile(rf"^{escaped}(?:\s*\(\d{{4}}\))?$", _re.IGNORECASE)
        pat_prefix = _re.compile(rf"^\(\d{{4}}\)\s*{escaped}$", _re.IGNORECASE)
        # Also match "Year - Album" format used by the default folder_structure_template
        pat_year_dash = _re.compile(rf"^\d{{4}}\s*[-–—]\s*{escaped}$", _re.IGNORECASE)
        for child in parent.iterdir():
            if child.is_dir() and (
                pat_suffix.match(child.name)
                or pat_prefix.match(child.name)
                or pat_year_dash.match(child.name)
            ):
                return child
        return None

    def _format_filename(
        self,
        track: TrackMetadata,
        track_number: Optional[int],
        *,
        disc_number: Optional[int] = None,
    ) -> str:
        return build_track_stem(track, self.filename_preferences, track_number=track_number, disc_number=disc_number)

    def is_already_downloaded(self, track: TrackMetadata) -> Optional[str]:
        """Return canonical file path if the track already exists in the library.

        In smart_dedup mode (default): checks the global identity index first
        (ISRC, Spotify ID, title+artist keys) — finds the track anywhere in the
        library regardless of which album folder it lives in.

        In full_albums mode: skips the cross-library index and only checks
        whether a file already exists at the exact target path for this track.
        This lets the same track appear in multiple album folders (e.g. studio
        album and a Best Of compilation) without one blocking the other.
        """
        # tag_only is skip for DOWNLOAD purposes — it never re-downloads a track
        # skip would have found (v2.0.0 DMX-5). Whether the found file is also
        # re-tagged is decided separately by retag_target().
        if self.conflict_behavior in ("skip", "tag_only") and not self.full_albums:
            for key in self._track_identity_keys(track):
                existing = self._identity_index.get(key)
                if existing and os.path.exists(existing):
                    if self._is_positively_different_recording(track, existing):
                        logger.debug(
                            'Identity hit %s ignored - ISRC says a different '
                            'recording (%s)', key, existing,
                        )
                        continue
                    return existing

        # Check the expected canonical path for this exact request.
        base = self.get_output_path(track)
        for ext in SUPPORTED_AUDIO_EXTENSIONS:
            candidate = base + ext
            if os.path.exists(extended_path(candidate)):
                if self.conflict_behavior == "overwrite":
                    return None
                if not self._file_matches_track_identity(track, Path(candidate)):
                    continue
                self._mark_done(track, candidate)
                return candidate

        return None

    @property
    def conflict_behavior(self) -> str:
        """skip | overwrite | append_counter | tag_only (v2.0.0 DMX-5)."""
        return str(self.filename_preferences.get("filename_conflict_behavior") or "skip")

    def retag_target(self, track: TrackMetadata, existing: str) -> Optional[str]:
        """The file tag_only should re-tag, or None to leave it untouched.

        Only a file sitting at THIS request's own target path is re-tagged. A
        match found elsewhere by the global identity index is the same recording
        filed under a different release (a Best Of vs the studio album) — writing
        this request's album/year/track tags onto it would corrupt that other
        release, so it is skipped exactly as `skip` would skip it.

        A hard-linked playlist copy is also left alone: its inode is shared with
        the album file, so re-tagging it with playlist-context metadata would
        silently rewrite the album copy too.
        """
        if self.conflict_behavior != "tag_only" or not existing:
            return None
        try:
            target_base = os.path.normcase(str(Path(self.get_output_path(track)).resolve()))
            found = Path(existing).resolve()
            found_base = os.path.normcase(str(found.with_suffix("")))
        except (OSError, ValueError):
            return None
        if found_base != target_base:
            return None
        if found.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            return None
        try:
            if track.playlist_name and os.stat(extended_path(str(found))).st_nlink > 1:
                logger.info(
                    "  [RETAG]  Not re-tagging hard-linked playlist copy (shared with its album file): %s",
                    found.name,
                )
                return None
        except OSError:
            return None
        return str(found)

    def mark_downloaded(self, track: TrackMetadata, file_path: str):
        self._mark_done(track, file_path)

    def mark_failed(self, track: TrackMetadata, reason: str):
        for key in self._track_identity_keys(track):
            self._state[f"{FAILED_PREFIX}{key}"] = reason
        self._save_state()

    def ensure_playlist_copy(self, track: TrackMetadata, canonical_path: str) -> str:
        """Return this track's file inside the playlist folder, making one if needed.

        **issue #75.** This used to ask one question only — "is there a file at
        the stem the CURRENT playlist position renders?" — and hard-link a second
        copy when the answer was no. For Spotify's Liked Songs, which prepends,
        every position shifts by one on every sync, so the answer was no for
        every already-downloaded track and the folder grew by one file per track
        per run (measured 3 -> 7 -> 12 files for 3 -> 4 -> 5 tracks). Dedup was
        working the whole time; this call undid it one line later.

        The question it asks now is "is this track ALREADY in the playlist
        folder?", which is position-independent. Renaming the file to its new
        number is a separate, later pass (renumber_playlist_folder), so that a
        user who turns renumbering off gets stale numbers rather than duplicates.
        """
        if not track.playlist_name or not canonical_path or not os.path.exists(canonical_path):
            return canonical_path
        if self.playlist_in_library_mode:
            # The canonical path IS the playlist path; there is nothing to copy.
            return canonical_path

        folder = self.playlist_folder_for(track.playlist_name)
        ext = Path(canonical_path).suffix

        # Cheapest answer first, and the one that covers the reported case: the
        # file dedup just found is itself sitting in this playlist's folder, so
        # it is the playlist copy no matter what number it carries.
        if self._is_in_folder(canonical_path, folder):
            return canonical_path

        playlist_path = self.get_output_path(track) + ext
        if os.path.abspath(playlist_path) == os.path.abspath(canonical_path):
            return canonical_path
        if os.path.exists(extended_path(playlist_path)):
            return playlist_path

        # The track was filed elsewhere (downloaded as part of its album) and the
        # playlist folder may still hold a copy under an OLD number. Only now is
        # the folder scan worth its cost.
        existing = self._playlist_copy_in_folder(track, folder)
        if existing:
            return existing

        os.makedirs(os.path.dirname(os.path.abspath(playlist_path)), exist_ok=True)
        try:
            os.link(canonical_path, playlist_path)
        except OSError:
            shutil.copy2(canonical_path, playlist_path)
        self._remember_playlist_file(track, folder, playlist_path)
        return playlist_path

    @staticmethod
    def _is_in_folder(file_path: str, folder: Path) -> bool:
        """True when file_path sits DIRECTLY in folder (not in a subfolder)."""
        try:
            return os.path.normcase(str(Path(file_path).resolve().parent)) == os.path.normcase(
                str(folder.resolve())
            )
        except (OSError, ValueError):
            return False

    def _playlist_copy_in_folder(self, track: TrackMetadata, folder: Path) -> Optional[str]:
        """This track's existing file inside `folder`, whatever number it carries."""
        index = self._playlist_folder_identity_index(folder)
        if not index:
            return None
        for key in self._track_identity_keys(track):
            found = index.get(key)
            if found and os.path.exists(extended_path(found)):
                if self._is_positively_different_recording(track, found):
                    continue
                return found
        return None

    def _playlist_folder_identity_index(self, folder: Path) -> dict[str, str]:
        """identity key -> path, for the audio files directly inside `folder`.

        Built at most once per folder per run. Non-recursive on purpose: a
        playlist folder is flat, and recursing would pull in an unrelated
        library sitting under the same root.
        """
        try:
            cache_key = os.path.normcase(str(folder.resolve()))
        except (OSError, ValueError):
            return {}
        cached = self._playlist_folder_index.get(cache_key)
        if cached is not None:
            return cached

        index: dict[str, str] = {}
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            self._playlist_folder_index[cache_key] = index
            return index

        for path in entries:
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            try:
                keys = self._extract_identity_keys_from_file(path)
            except Exception as e:
                logger.debug(f"Playlist folder scan skipped {path}: {e}")
                continue
            resolved = str(path.resolve())
            for key in keys:
                index.setdefault(key, resolved)

        self._playlist_folder_index[cache_key] = index
        return index

    def _remember_playlist_file(self, track: TrackMetadata, folder: Path, path: str) -> None:
        """Fold a file this run just created into the folder index.

        Without this, two tracks resolving to the same identity within one run
        (the same recording twice in one playlist) would each create a copy.
        """
        try:
            cache_key = os.path.normcase(str(folder.resolve()))
        except (OSError, ValueError):
            return
        index = self._playlist_folder_index.get(cache_key)
        if index is None:
            return
        resolved = str(Path(path).resolve())
        for key in self._track_identity_keys(track):
            index.setdefault(key, resolved)

    # ── issue #75 — keeping a playlist folder in step with the playlist ────

    def renumber_playlist_folder(
        self, playlist_name: str, entries: list[tuple[TrackMetadata, str]]
    ) -> dict[str, str]:
        """Rename this playlist's files so their numbers match current positions.

        Returns {old path: new path} for the renames that actually happened, so
        the caller can point the .m3u (and its own results) at the new names.

        This is the half of issue #75 the reporter asked for by name. The other
        half (ensure_playlist_copy) stops duplicates being created; this stops
        the numbers going stale, which is the only reason to put {track} in a
        playlist filename at all.

        Renaming is done in TWO PHASES through temporary names. A playlist that
        gained one track at the top wants 01->02, 02->03, 03->04 — every
        destination is occupied by another file in the same batch, so a direct
        pass would either refuse or clobber depending on the platform. Staging
        every source out of the way first makes shifts and cycles both work.

        A destination held by a file that is NOT part of this batch is never
        overwritten; that rename is skipped and logged. Orphan pruning runs
        first for exactly this reason.
        """
        if not playlist_name or not self.playlist_renumber or self.playlist_in_library_mode:
            return {}
        folder = self.playlist_folder_for(playlist_name)
        if not folder.is_dir():
            return {}
        try:
            folder_res = folder.resolve()
        except OSError:
            return {}

        plan: list[tuple[Path, Path]] = []
        sources: set[str] = set()
        claimed: set[str] = set()
        for track, path in entries:
            if not path or not self._is_in_folder(path, folder):
                continue
            current = Path(path)
            try:
                if not current.exists():
                    continue
                current = current.resolve()
            except OSError:
                continue
            desired = folder_res / (self.playlist_stem_for(track) + current.suffix)
            current_key = os.path.normcase(str(current))
            desired_key = os.path.normcase(str(desired))
            if current_key == desired_key or desired_key in claimed:
                # Already right, or another track in this run claimed the name
                # first (two copies of one recording, or duplicate positions).
                claimed.add(current_key)
                continue
            plan.append((current, desired))
            sources.add(current_key)
            claimed.add(desired_key)

        # Drop any rename whose destination belongs to a file this batch does
        # not own. Second pass, because `sources` is only complete now.
        runnable = [
            (current, desired)
            for current, desired in plan
            if not (
                os.path.exists(extended_path(str(desired)))
                and os.path.normcase(str(desired)) not in sources
            )
        ]
        skipped = len(plan) - len(runnable)
        if skipped:
            logger.info(
                f"  [RENUM]  {skipped} file(s) left alone: another file already holds the name"
            )
        if not runnable:
            return {}

        renamed = self._two_phase_rename(folder_res, runnable)
        if renamed:
            self._repath(renamed)
            logger.info(f"  [RENUM]  Renumbered {len(renamed)} file(s) in {folder.name}")
        return renamed

    def _two_phase_rename(
        self, folder: Path, moves: list[tuple[Path, Path]]
    ) -> dict[str, str]:
        """Stage every source aside, then drop them onto their destinations.

        Rolls the whole batch back on the first failure. A half-renumbered folder
        is worse than an un-renumbered one: the numbers would no longer describe
        either the old order or the new one.
        """
        staged: list[tuple[Path, Path, Path]] = []  # (original, temp, desired)
        placed: list[tuple[Path, Path]] = []        # (desired, original)
        try:
            for i, (current, desired) in enumerate(moves):
                temp = folder / f"{_RENUMBER_TMP_SUFFIX}-{i}.tmp"
                os.replace(extended_path(str(current)), extended_path(str(temp)))
                staged.append((current, temp, desired))
            for current, temp, desired in staged:
                os.replace(extended_path(str(temp)), extended_path(str(desired)))
                placed.append((desired, current))
        except OSError as e:
            logger.warning(f"  [RENUM]  Renumbering failed, rolling back: {e}")
            for desired, current in reversed(placed):
                try:
                    os.replace(extended_path(str(desired)), extended_path(str(current)))
                except OSError:
                    logger.warning(f"  [RENUM]  Could not restore {current.name}")
            placed_set = {os.path.normcase(str(d)) for d, _ in placed}
            for current, temp, desired in staged:
                if os.path.normcase(str(desired)) in placed_set:
                    continue
                try:
                    os.replace(extended_path(str(temp)), extended_path(str(current)))
                except OSError:
                    logger.warning(f"  [RENUM]  Could not restore {current.name}")
            return {}
        return {str(current): str(desired) for current, _temp, desired in staged}

    def _repath(self, renamed: dict[str, str]) -> None:
        """Point the dedup state and in-memory indexes at the renamed files.

        Without this the next call in the same run would look up a path that no
        longer exists, and the on-disk state file would keep sending later runs
        to the old name.
        """
        lookup = {os.path.normcase(old): new for old, new in renamed.items()}

        def _moved(value: str) -> Optional[str]:
            return lookup.get(os.path.normcase(str(value)))

        for key, value in list(self._identity_index.items()):
            new = _moved(value)
            if new:
                self._identity_index[key] = new
        for key, value in list(self._state.items()):
            if isinstance(value, str) and key.startswith(TRACK_KEY_PREFIX):
                new = _moved(value)
                if new:
                    self._state[key] = new
        for folder_index in self._playlist_folder_index.values():
            for key, value in list(folder_index.items()):
                new = _moved(value)
                if new:
                    folder_index[key] = new
        self._save_state()

    def find_playlist_orphans(
        self, playlist_name: str, keep_paths: list[str]
    ) -> list[str]:
        """Audio files in the playlist folder that the playlist no longer contains.

        Read-only — it never deletes. Deleting is an explicit user action, or
        `playlist_prune_orphans="auto"` in prune_playlist_orphans below.

        **The caller must only pass a COMPLETE playlist run.** On a partial run
        (a PV-1 selection, an auto-sync that fetched only new tracks, a cancelled
        job) every track that was not part of the run looks exactly like an
        orphan, and this would offer the user their whole library to delete.
        """
        if not playlist_name or self.playlist_in_library_mode:
            return []
        folder = self.playlist_folder_for(playlist_name)
        if not folder.is_dir():
            return []
        keep: set[str] = set()
        for path in keep_paths:
            if not path:
                continue
            try:
                keep.add(os.path.normcase(str(Path(path).resolve())))
            except (OSError, ValueError):
                continue
        orphans: list[str] = []
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return []
        for path in entries:
            try:
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue
                if os.path.normcase(str(path.resolve())) in keep:
                    continue
            except OSError:
                continue
            orphans.append(str(path))
        return orphans

    def prune_playlist_orphans(self, orphans: list[str]) -> list[str]:
        """Delete the given orphans. Returns the paths actually removed.

        Kept separate from find_playlist_orphans so that reporting and deleting
        are different calls: the default mode reports and stops.
        """
        removed: list[str] = []
        for path in orphans:
            try:
                os.remove(extended_path(path))
                removed.append(path)
            except OSError as e:
                logger.warning(f"  [PRUNE]  Could not remove {os.path.basename(path)}: {e}")
        if removed:
            self._forget_paths(removed)
        return removed

    def _forget_paths(self, paths: list[str]) -> None:
        """Drop deleted files from the dedup state and in-memory indexes."""
        gone = {os.path.normcase(str(p)) for p in paths}

        def _is_gone(value) -> bool:
            return isinstance(value, str) and os.path.normcase(value) in gone

        for key, value in list(self._identity_index.items()):
            if _is_gone(value):
                del self._identity_index[key]
        for key, value in list(self._state.items()):
            if key.startswith(TRACK_KEY_PREFIX) and _is_gone(value):
                del self._state[key]
        for folder_index in self._playlist_folder_index.values():
            for key, value in list(folder_index.items()):
                if _is_gone(value):
                    del folder_index[key]
        self._save_state()

    def write_playlist_manifest(self, playlist_name: str, file_paths: list[str]) -> str:
        manifest_root = self.root if self.playlist_folder_structure == "flat" else self.playlists_root
        manifest_path = manifest_root / f"{self._safe(playlist_name)}.m3u"
        lines = ["#EXTM3U"]
        for file_path in file_paths:
            if not file_path:
                continue
            relative = os.path.relpath(file_path, manifest_path.parent)
            lines.append(Path(relative).as_posix())
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(manifest_path)

    def _resolve_conflict_path(self, folder: Path, stem: str, track: Optional[TrackMetadata] = None) -> Path:
        behavior = self.filename_preferences.get("filename_conflict_behavior")
        if behavior != "append_counter":
            if not track:
                return folder / stem
            existing_paths = [
                folder / f"{stem}{ext}"
                for ext in SUPPORTED_AUDIO_EXTENSIONS
                if os.path.exists(extended_path(str(folder / f"{stem}{ext}")))
            ]
            if not existing_paths:
                return folder / stem
            # Only allocate a numbered duplicate when an existing file is
            # POSITIVELY a different track. "Could not identify it" must not
            # produce a second copy — that is what turned 201 tracks into 219
            # (v1.1.8 BUG-3).
            if not all(
                self._existing_file_is_positively_different(track, path)
                for path in existing_paths
            ):
                return folder / stem
            counter = 2
            while any(os.path.exists(extended_path(str(folder / f"{stem} ({counter}){ext}"))) for ext in SUPPORTED_AUDIO_EXTENSIONS):
                counter += 1
            return folder / f"{stem} ({counter})"

        candidate = folder / stem
        if not any(os.path.exists(extended_path(str(folder / f"{stem}{ext}"))) for ext in SUPPORTED_AUDIO_EXTENSIONS):
            return candidate
        counter = 2
        while any(os.path.exists(extended_path(str(folder / f"{stem} ({counter}){ext}"))) for ext in SUPPORTED_AUDIO_EXTENSIONS):
            counter += 1
        return folder / f"{stem} ({counter})"

    # ── State / identity helpers ──────────────────────────────────────────

    def _mark_done(self, track: TrackMetadata, path: str):
        resolved = str(Path(path).resolve())
        self._remember_path_isrc(resolved, track.isrc)
        for key in self._track_identity_keys(track):
            self._state[f"{TRACK_KEY_PREFIX}{key}"] = resolved
            self._identity_index[key] = resolved
        self._save_state()

    def _identity_text_variants(self, track: TrackMetadata) -> list[tuple[str, list[str]]]:
        """(title, artists) pairs this track could be filed under.

        Always includes the raw catalogue strings. When a v2.0.0 DMX-2 title
        transform is active it also includes the TRANSFORMED strings, because
        those are what actually get written into the file's tags — and
        _identity_keys_from_values reads the file, so without this the two
        sides could never intersect and the track would re-download forever.

        Casing is deliberately not a trigger (_normalize_identity_part
        lowercases, so it cannot change a key) and neither is duplicate-artist
        removal (_artists_canonical_key builds a set, and primary_artist is
        artists[0], which first-occurrence dedup preserves).
        """
        raw = (track.title or "", list(track.artists or []))
        prefs = self.filename_preferences or {}
        if not (
            prefs.get("remove_album_version", False)
            or str(prefs.get("featured_artists_mode", "nothing") or "nothing").lower()
            in ("remove", "move")
        ):
            return [raw]
        try:
            tx = transform_text(track, prefs)
        except Exception:
            return [raw]
        transformed = (tx.title or "", list(tx.artists or []))
        return [raw] if transformed == raw else [raw, transformed]

    def _track_identity_keys(self, track: TrackMetadata) -> list[str]:
        keys: list[str] = []
        if track.isrc:
            keys.append(f"isrc:{track.isrc.strip().lower()}")
        if track.spotify_id:
            keys.append(f"spotify:{track.spotify_id.strip()}")
        # Must mirror the file-side key built in _identity_keys_from_values, or
        # the two sets can never intersect and the signal is dead weight
        # (v1.1.8 FEAT-6). UPC is album-level, so it only identifies a track when
        # combined with BOTH disc and track number — with track number alone,
        # disc 1 track 1 and disc 2 track 1 of the same release produce the same
        # key, and the second disc is skipped as "already downloaded" against a
        # file that is a completely different song (v1.1.8 BUG-10).
        _upc = getattr(track, "upc", None)
        if _upc and track.track_number:
            # NB: the re.sub is hoisted out of the f-string deliberately — a
            # backslash inside an f-string expression is only legal from Python
            # 3.12 (PEP 701) and is a SyntaxError on 3.11, which is what CI
            # builds with. PyInstaller silently omits a module it cannot
            # compile, which shipped three broken v1.1.8 releases.
            _upc_digits = re.sub(r"\D", "", str(_upc))
            keys.append(
                f"upc:{_upc_digits}"
                f":{int(track.disc_number or 1)}:{int(track.track_number)}"
            )

        album_key = self._normalize_identity_part(track.album)

        # One pass per (title, artists) variant — the raw catalogue strings
        # plus, when a DMX-2 title transform is active, the strings actually
        # written to the file. dict.fromkeys below dedupes, so the common case
        # (one variant) produces exactly the keys it did before DMX-2.
        for variant_title, variant_artists in self._identity_text_variants(track):
            title_key = self._normalize_identity_part(variant_title)
            artist_key = self._normalize_identity_part(
                variant_artists[0] if variant_artists else track.primary_artist
            )
            if title_key and artist_key:
                keys.append(f"title_artist:{title_key}:{artist_key}")
            if title_key and artist_key and album_key and album_key != "unknown album":
                keys.append(f"title_artist_album:{title_key}:{artist_key}:{album_key}")

            # Source-independent all-artists key: splits combined strings like
            # "Future & Metro Boomin", normalizes each part, sorts them — so
            # ["Future", "Metro Boomin"], ["Metro Boomin", "Future"], and
            # ["Future & Metro Boomin"] (single combined tag) all produce the
            # same key.
            if variant_artists:
                canonical = self._artists_canonical_key(variant_artists)
                if title_key and canonical:
                    keys.append(f"title_artists:{title_key}:{canonical}")

        return list(dict.fromkeys(keys))

    def _build_identity_index(self):
        for key, path in self._load_track_entries_from_state().items():
            if os.path.exists(path):
                self._identity_index.setdefault(key, path)
                if key.startswith('isrc:'):
                    self._remember_path_isrc(path, key[len('isrc:'):])

        # v2.0.0 SF-4 — a file whose (mtime, size) is unchanged since the last
        # run is served from the cache instead of being re-parsed by mutagen.
        # The rglob order and the setdefault semantics are unchanged, so when
        # two files share an identity key the same one still wins.
        index = self._library_index
        cached = {}
        if index is not None:
            cached = index.load_root(self._index_root, self._index_version)
        seen: set[str] = set()
        pending: list = []
        hits = 0
        misses = 0

        for file_path in self.root.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue

            stat = None
            path_key = None
            if index is not None:
                path_key = str(file_path)
                seen.add(path_key)
                try:
                    stat = file_path.stat()
                except Exception:
                    stat = None

            row = cached.get(path_key) if (stat is not None and path_key) else None
            if row is not None and row[0] == stat.st_mtime_ns and row[1] == stat.st_size:
                resolved, identity_keys = row[2], row[3]
                hits += 1
            else:
                try:
                    identity_keys = self._extract_identity_keys_from_file(file_path)
                except Exception as e:
                    logger.debug(f"Library scan skipped {file_path}: {e}")
                    continue
                resolved = str(file_path.resolve())
                misses += 1
                if stat is not None and path_key:
                    pending.append(
                        (path_key, stat.st_mtime_ns, stat.st_size, resolved, identity_keys)
                    )

            for key in identity_keys:
                self._identity_index.setdefault(key, resolved)
            # Free: the keys were just read (or served from the SF-4
            # cache), so the veto costs no extra I/O for a library file.
            self._remember_path_isrc(
                resolved, self._isrc_from_identity_keys(identity_keys)
            )

        if index is not None:
            if pending:
                index.store(self._index_root, self._index_version, pending)
            index.prune(self._index_root, seen)
            if hits or misses:
                logger.debug(
                    f"[LibraryIndex] {hits} cached, {misses} read ({self._index_root})"
                )

    def _load_track_entries_from_state(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for key, value in self._state.items():
            if not isinstance(value, str):
                continue
            if key.startswith(FAILED_PREFIX):
                continue
            if key.startswith(TRACK_KEY_PREFIX):
                entries[key[len(TRACK_KEY_PREFIX):]] = value
                continue

            # Legacy state support
            canonical_key = self._legacy_state_key_to_identity(key)
            if canonical_key:
                entries[canonical_key] = value
        return entries

    @staticmethod
    def _legacy_state_key_to_identity(key: str) -> Optional[str]:
        if key.startswith("playlist:"):
            if ":spotify:" in key:
                return f"spotify:{key.split(':spotify:', 1)[1]}"
            return None
        if key.startswith("isrc:") or key.startswith("spotify:") or key.startswith("title:"):
            return key
        return None

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                # Keep the unreadable file for diagnosis: the next save would otherwise
                # overwrite it with a state that has forgotten every earlier download.
                try:
                    os.replace(self._state_path, str(self._state_path) + ".corrupt")
                    logger.warning("Download state was unreadable; kept as .corrupt and starting fresh")
                except OSError:
                    pass
        return {}

    def _save_state(self):
        try:
            # .copy() is atomic under the GIL; dumping the live dict raced with other
            # workers' inserts ("dictionary changed size during iteration"), which this
            # handler swallowed - silently dropping that save.
            data = json.dumps(self._state.copy(), indent=2)
            # Serialised + temp-file/rename: parallel workers all call this, and a plain
            # write_text truncates first, so an overlap or a Stop mid-write left a torn
            # file that _load_state then read as "nothing downloaded yet".
            with _STATE_WRITE_LOCK:
                tmp = self._state_path.with_name(self._state_path.name + ".tmp")
                tmp.write_text(data, encoding="utf-8")
                try:
                    # Windows refuses to replace a file another handle has open (an
                    # indexer, antivirus or backup tool reading it): retry briefly.
                    for attempt in range(20):
                        try:
                            os.replace(tmp, self._state_path)
                            break
                        except PermissionError:
                            if attempt == 19:
                                raise
                            time.sleep(0.01)
                except BaseException:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    # ── File scan helpers ─────────────────────────────────────────────────

    def _extract_identity_keys_from_file(self, path: Path) -> list[str]:
        ext = path.suffix.lower()
        if ext == ".flac":
            return self._extract_flac_identity_keys(path)
        if ext == ".mp3":
            return self._extract_mp3_identity_keys(path)
        if ext in {".m4a", ".mp4"}:
            return self._extract_mp4_identity_keys(path)
        return self._extract_filename_identity_keys(path)

    # -- ISRC veto ---------------------------------------------------------
    # A fuzzy title+artist hit means 'these could be the same recording'.
    # Two catalogue ISRCs that disagree mean 'they are definitely not', and
    # that is the stronger statement, so it wins. Anything unknown on either
    # side must stay silent: 'I could not tell' is not 'different', or an
    # untagged file becomes grounds for a second copy (v1.1.8 BUG-3).

    @staticmethod
    def _isrc_from_identity_keys(keys) -> Optional[str]:
        for key in keys or ():
            if key.startswith('isrc:'):
                return key[len('isrc:'):].strip().upper() or None
        return None

    @staticmethod
    def _isrcs_disagree(track: TrackMetadata, other: Optional[str]) -> bool:
        ours = (track.isrc or '').strip().upper()
        return bool(ours and other and ours != other.strip().upper())

    def _remember_path_isrc(self, path: str, isrc: Optional[str]) -> None:
        value = (isrc or '').strip().upper()
        if not value:
            return
        try:
            self._path_isrc.setdefault(
                os.path.normcase(str(Path(path).resolve())), value
            )
        except (OSError, ValueError):
            pass

    def _known_isrc_for_path(self, path: str) -> Optional[str]:
        """The ISRC of an existing file, from memory or from its own tags."""
        try:
            cache_key = os.path.normcase(str(Path(path).resolve()))
        except (OSError, ValueError):
            return None
        cached = self._path_isrc.get(cache_key)
        if cached:
            return cached
        try:
            found = self._isrc_from_identity_keys(
                self._extract_identity_keys_from_file(Path(path))
            )
        except Exception as e:
            logger.debug('Could not read ISRC from %s: %s', path, e)
            return None
        if found:
            self._path_isrc[cache_key] = found
        return found

    def _is_positively_different_recording(
        self, track: TrackMetadata, existing: str
    ) -> bool:
        if not (track.isrc or '').strip():
            return False
        return self._isrcs_disagree(track, self._known_isrc_for_path(existing))

    def _file_matches_track_identity(self, track: TrackMetadata, path: Path) -> bool:
        """Return True only when an existing file is the same logical track.

        A path collision alone is not enough to skip. Playlists can contain
        distinct covers with the same title, and title-only filename templates
        render those tracks to the same stem.
        """
        try:
            existing_keys = set(self._extract_identity_keys_from_file(path))
        except Exception as e:
            logger.debug(f"Could not inspect existing file identity for {path}: {e}")
            return False
        if self._isrcs_disagree(track, self._isrc_from_identity_keys(existing_keys)):
            return False
        return bool(existing_keys.intersection(self._track_identity_keys(track)))

    def _existing_file_is_positively_different(self, track: TrackMetadata, path: Path) -> bool:
        """True ONLY when the existing file can be positively identified as a
        different track (v1.1.8 BUG-3).

        The distinction between *"this is a different track"* and *"I could not
        tell"* is the whole point. `_file_matches_track_identity` collapses both
        to False, which is correct where the consequence is "download it again"
        but wrong where the consequence is "write a second copy alongside it":
        an existing file that is unreadable, or that carries no usable tags
        (exactly what a file left behind by failed tagging looks like), was being
        treated as proof of a different recording and got a ` (2)` duplicate.

        A genuinely distinct same-titled track normally has readable tags, so the
        case this guard exists for — distinct covers colliding on a title-only
        filename template — still works.
        """
        try:
            existing_keys = set(self._extract_identity_keys_from_file(path))
        except Exception as e:
            logger.debug(f"Could not inspect existing file identity for {path}: {e}")
            return False  # unknown — never grounds for a duplicate
        if not existing_keys:
            logger.debug(f"Existing file has no usable identity tags, assuming ours: {path}")
            return False  # unknown — never grounds for a duplicate
        if self._isrcs_disagree(track, self._isrc_from_identity_keys(existing_keys)):
            # Two catalogue ISRCs that disagree are proof, and they
            # outrank the fuzzy title keys below - which a live or
            # acoustic re-cut shares with the studio version.
            return True
        return not existing_keys.intersection(self._track_identity_keys(track))

    def _extract_flac_identity_keys(self, path: Path) -> list[str]:
        audio = FLAC(path)
        title = self._first(audio.get("title"))
        artists = audio.get("artist", [])
        album = self._first(audio.get("album"))
        isrc = self._first(audio.get("isrc"))
        spotify_id = self._first(audio.get("spotify_id"))
        upc = self._first_upc(dict(audio))
        track_number = self._parse_track_number(self._first(audio.get("tracknumber")))
        disc_number = self._parse_track_number(self._first(audio.get("discnumber")))
        return self._identity_keys_from_values(
            title, artists, album, isrc, spotify_id, upc, track_number, disc_number
        )

    def _extract_mp3_identity_keys(self, path: Path) -> list[str]:
        audio = ID3(path)
        title = self._id3_text(audio.getall("TIT2"))
        artists = self._id3_text_list(audio.getall("TPE1"))
        album = self._id3_text(audio.getall("TALB"))
        isrc = self._id3_text(audio.getall("TSRC"))
        spotify_id = self._id3_txxx(audio, "SPOTIFYID")
        upc = self._id3_txxx(audio, "UPC") or self._id3_txxx(audio, "BARCODE")
        track_number = self._parse_track_number(self._id3_text(audio.getall("TRCK")))
        disc_number = self._parse_track_number(self._id3_text(audio.getall("TPOS")))
        return self._identity_keys_from_values(
            title, artists, album, isrc, spotify_id, upc, track_number, disc_number
        )

    @staticmethod
    def _parse_track_number(value) -> Optional[int]:
        """Parse a track number from any of the shapes tags use: 7, "7", "7/12"."""
        if value is None:
            return None
        try:
            text = str(value).strip()
            if not text:
                return None
            text = text.split("/")[0].strip()
            return int(text) or None
        except (TypeError, ValueError):
            return None

    def _extract_mp4_identity_keys(self, path: Path) -> list[str]:
        audio = MP4(path)
        title = self._first(audio.get("\xa9nam"))
        artists = [value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
                   for value in audio.get("\xa9ART", [])]
        album = self._first(audio.get("\xa9alb"))
        isrc = self._first_freeform(audio, "ISRC")
        spotify_id = self._first_freeform(audio, "SPOTIFYID")
        upc = self._first_freeform(audio, "UPC") or self._first_freeform(audio, "BARCODE")
        track_number = None
        try:
            trkn = audio.get("trkn")
            if trkn and isinstance(trkn[0], (list, tuple)) and trkn[0]:
                track_number = int(trkn[0][0]) or None
        except Exception:
            track_number = None
        disc_number = None
        try:
            disk = audio.get("disk")
            if disk and isinstance(disk[0], (list, tuple)) and disk[0]:
                disc_number = int(disk[0][0]) or None
        except Exception:
            disc_number = None
        return self._identity_keys_from_values(
            title, artists, album, isrc, spotify_id, upc, track_number, disc_number
        )

    def _extract_filename_identity_keys(self, path: Path) -> list[str]:
        stem = path.stem
        stem = re.sub(r"^\d+\s*-\s*", "", stem).strip()
        title = stem or "Unknown Track"
        return self._identity_keys_from_values(title, [], None, None, None)

    # UPC/barcode appears under many different tag spellings depending on which
    # tool wrote the file (v1.1.8 FEAT-6).
    # We already WRITE `barcode`, but only ever read our own spelling back, so a
    # library containing files tagged by other tools lost the signal entirely.
    _UPC_TAG_KEYS = (
        "upc", "barcode",
        "txxx:upc", "txxx:barcode", "txxx/upc", "txxx/barcode",
        "wm/upc",
        "----:com.apple.itunes:upc", "----:com.apple.itunes:barcode",
    )

    @classmethod
    def _first_upc(cls, tags: dict) -> Optional[str]:
        """Return the first UPC/barcode found under any known spelling.

        `tags` is a case-insensitive-ish mapping of tag name -> value(s).
        """
        if not tags:
            return None
        lowered = {}
        for k, v in tags.items():
            try:
                lowered[str(k).strip().lower()] = v
            except Exception:
                continue
        for key in cls._UPC_TAG_KEYS:
            val = lowered.get(key)
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                val = val[0] if val else None
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="ignore")
            val = str(val or "").strip()
            # Keep digits only — barcodes are written with stray spaces/hyphens.
            digits = re.sub(r"\D", "", val)
            if digits:
                return digits
        return None

    def _identity_keys_from_values(
        self,
        title: Optional[str],
        artists: list[str],
        album: Optional[str],
        isrc: Optional[str],
        spotify_id: Optional[str],
        upc: Optional[str] = None,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
    ) -> list[str]:
        keys: list[str] = []
        if isrc:
            keys.append(f"isrc:{isrc.strip().lower()}")
        # UPC alone is an ALBUM identifier, so it is only a track identity when
        # combined with disc AND track number — otherwise every track on a
        # release would collide with every other, and on a multi-disc release
        # disc 2 track N collides with disc 1 track N (v1.1.8 BUG-10).
        if upc and track_number:
            # Hoisted out of the f-string: see the note on the matching
            # in-memory key above (backslash in an f-string expression is
            # 3.12+ only and a SyntaxError on the 3.11 CI builds with).
            _upc_digits = re.sub(r"\D", "", str(upc))
            keys.append(
                f"upc:{_upc_digits}"
                f":{int(disc_number or 1)}:{int(track_number)}"
            )
        if spotify_id:
            keys.append(f"spotify:{spotify_id.strip()}")

        title_key = self._normalize_identity_part(title)
        artist_key = self._normalize_identity_part(artists[0] if artists else None)
        album_key = self._normalize_identity_part(album)

        if title_key and artist_key:
            keys.append(f"title_artist:{title_key}:{artist_key}")
        if title_key and artist_key and album_key and album_key != "unknown album":
            keys.append(f"title_artist_album:{title_key}:{artist_key}:{album_key}")
        elif title_key:
            keys.append(f"title:{title_key}")

        # All-artists key: same logic as _track_identity_keys.
        # When the file has a combined tag like artist = ["Future & Metro Boomin"],
        # _artists_canonical_key splits it to the same key as ["Future", "Metro Boomin"].
        if artists:
            canonical = self._artists_canonical_key(artists)
            if title_key and canonical:
                keys.append(f"title_artists:{title_key}:{canonical}")

        return list(dict.fromkeys(keys))

    @staticmethod
    def _first(values) -> Optional[str]:
        if not values:
            return None
        value = values[0]
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    @staticmethod
    def _id3_text(frames) -> Optional[str]:
        if not frames:
            return None
        texts = getattr(frames[0], "text", None) or []
        return str(texts[0]) if texts else None

    @staticmethod
    def _id3_text_list(frames) -> list[str]:
        if not frames:
            return []
        texts = getattr(frames[0], "text", None) or []
        return [str(text) for text in texts if text]

    @staticmethod
    def _id3_txxx(audio: ID3, desc: str) -> Optional[str]:
        for frame in audio.getall("TXXX"):
            if getattr(frame, "desc", "") == desc:
                texts = getattr(frame, "text", None) or []
                return str(texts[0]) if texts else None
        return None

    @staticmethod
    def _first_freeform(audio: MP4, desc: str) -> Optional[str]:
        key = f"----:com.apple.iTunes:{desc}"
        values = audio.get(key, [])
        if not values:
            return None
        value = values[0]
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    @staticmethod
    def _normalize_identity_part(value: Optional[str]) -> str:
        if not value:
            return ""
        # Unicode-normalize BEFORE stripping (v1.1.8 BUG-3). Without this the
        # same title produces different identity keys depending on which
        # normalization form it arrives in — and macOS is where the two forms
        # meet, since HFS+/APFS work in NFD while tags and APIs generally carry
        # NFC. Stripping first meant:
        #     "Björk"  NFC (o-umlaut as one char) -> "bjrk"   (letter deleted)
        #     "Björk"  NFD (o + combining umlaut) -> "bjork"  (mark deleted)
        # Two keys for one track, so dedup silently failed on any accented
        # title. Decomposing first and dropping only the combining marks folds
        # both forms to "bjork".
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.lower()
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"[^a-z0-9 ]+", "", value)
        return value.strip()

    @staticmethod
    def _artists_canonical_key(artists: list[str]) -> str:
        """Return a source-independent sorted key for a set of artists.

        Splits combined strings (e.g. "Future & Metro Boomin") into individual
        names before normalizing and sorting, so different ways of expressing the
        same collaboration all produce the same key:
          ["Future", "Metro Boomin"]     → "future metro boomin"
          ["Metro Boomin", "Future"]     → "future metro boomin"
          ["Future & Metro Boomin"]      → "future metro boomin"
          ["Future", "Metro Boomin & X"] → "future metro boomin x"
        """
        parts: set[str] = set()
        for artist in artists:
            # Split on common multi-artist separators found in tags
            for part in re.split(r"[,&/]+|\s+(?:feat\.?|ft\.?)\s+", artist, flags=re.IGNORECASE):
                norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", "", part.lower())).strip()
                if norm:
                    parts.add(norm)
        return " ".join(sorted(parts))

    # ── Path sanitization ─────────────────────────────────────────────────

    @staticmethod
    def _safe(name: str, max_len: int = 80) -> str:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
        name = name.strip(". ")
        return name[:max_len] or "Unknown"


# Built after the class body so the methods exist. Any edit to one of these
# changes the cache version and invalidates every stored row.
_IDENTITY_LOGIC_FUNCTIONS = (
    LibraryOrganizer._identity_keys_from_values,
    LibraryOrganizer._normalize_identity_part,
    LibraryOrganizer._artists_canonical_key,
    LibraryOrganizer._extract_identity_keys_from_file,
    LibraryOrganizer._extract_flac_identity_keys,
    LibraryOrganizer._extract_mp3_identity_keys,
    LibraryOrganizer._extract_mp4_identity_keys,
    LibraryOrganizer._extract_filename_identity_keys,
    LibraryOrganizer._first_upc,
    LibraryOrganizer._parse_track_number,
)
