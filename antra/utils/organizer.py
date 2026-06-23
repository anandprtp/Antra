"""
Handles library layout, global deduplication, and playlist manifest generation.
"""
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TSRC, TXXX
from mutagen.mp4 import MP4

from antra.core.models import TrackMetadata
from antra_shared.filename_prefs import (
    build_folder_path,
    build_single_track_stem,
    build_track_stem,
    migrate_legacy_templates,
)

logger = logging.getLogger(__name__)

STATE_FILE = ".antra_state.json"
TRACK_KEY_PREFIX = "TRACK:"
FAILED_PREFIX = "FAILED:"
SUPPORTED_AUDIO_EXTENSIONS = (".flac", ".mp3", ".aac", ".m4a", ".mp4", ".opus")


class LibraryOrganizer:
    """
    Library structure (no Albums/ or Playlists/ wrappers):
      <root>/<Artist>/<Album (Year)>/<NN - Track Title>.<ext>   (standard mode)
      <root>/<Album (Year)>/<NN - Track Title>.<ext>            (flat mode)
      <root>/<Playlist Name>/<NN - Track Title>.<ext>
      <root>/<Playlist Name>.m3u

    Deduplication is global across the library. The first downloaded file path
    becomes canonical and later playlist/album/song downloads reuse that file.
    """

    def __init__(
        self,
        root: str,
        full_albums: bool = False,
        folder_structure: str = "standard",
        album_folder_structure: str = "",
        playlist_folder_structure: str = "",
        single_track_structure: str = "album_numbered",
        filename_format: str = "default",
        single_track_filename_template: str = "",
        album_track_filename_template: str = "",
        folder_structure_template: str = "",
        multi_disc_handling: str = "prefix",
        track_number_padding: int = 2,
        illegal_character_replacement: str = "",
        whitespace_handling: str = "preserve",
        filename_conflict_behavior: str = "skip",
    ):
        self.root = Path(root).resolve()
        self.full_albums = full_albums
        legacy_structure = folder_structure or "standard"
        self.folder_structure = legacy_structure
        self.album_folder_structure = album_folder_structure or legacy_structure
        self.playlist_folder_structure = playlist_folder_structure or legacy_structure
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
            },
            filename_format=filename_format,
            album_folder_structure=self.album_folder_structure,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        # No Albums/ or Playlists/ wrappers — everything lives directly under root.
        self.albums_root = self.root
        self.playlists_root = self.root
        self._state_path = self.root / STATE_FILE
        self._state = self._load_state()
        self._identity_index: dict[str, str] = {}
        if not full_albums:
            self._build_identity_index()

    # ── Public API ────────────────────────────────────────────────────────

    def get_output_path(self, track: TrackMetadata) -> str:
        """Return the target output path WITHOUT extension."""
        if track.playlist_name:
            playlist_dir = self._safe(track.playlist_name)
            track_number = track.playlist_position or track.track_number
            filename = self._format_filename(track, track_number, disc_number=1)
            if self.playlist_folder_structure == "flat":
                folder = self.root / playlist_dir
            else:
                folder = self.playlists_root / playlist_dir
            folder.mkdir(parents=True, exist_ok=True)
            return str(self._resolve_conflict_path(folder, filename, track=track))

        if (track.request_kind or "").lower() == "track":
            return self._single_track_output_path(track)

        folder = self._album_folder(track)
        filename = self._format_filename(track, track.track_number)
        folder.mkdir(parents=True, exist_ok=True)
        return str(self._resolve_conflict_path(folder, filename, track=track))

    def _single_track_output_path(self, track: TrackMetadata) -> str:
        if self.single_track_structure == "file":
            folder = self.root
            filename = build_single_track_stem(track, self.filename_preferences)
        else:
            folder = self._album_folder(track)
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
        if self.filename_preferences.get("filename_conflict_behavior") == "skip" and not self.full_albums:
            for key in self._track_identity_keys(track):
                existing = self._identity_index.get(key)
                if existing and os.path.exists(existing):
                    return existing

        # Check the expected canonical path for this exact request.
        base = self.get_output_path(track)
        for ext in SUPPORTED_AUDIO_EXTENSIONS:
            candidate = base + ext
            if os.path.exists(candidate):
                if self.filename_preferences.get("filename_conflict_behavior") == "overwrite":
                    return None
                if not self._file_matches_track_identity(track, Path(candidate)):
                    continue
                self._mark_done(track, candidate)
                return candidate

        return None

    def mark_downloaded(self, track: TrackMetadata, file_path: str):
        self._mark_done(track, file_path)

    def mark_failed(self, track: TrackMetadata, reason: str):
        for key in self._track_identity_keys(track):
            self._state[f"{FAILED_PREFIX}{key}"] = reason
        self._save_state()

    def ensure_playlist_copy(self, track: TrackMetadata, canonical_path: str) -> str:
        if not track.playlist_name or not canonical_path or not os.path.exists(canonical_path):
            return canonical_path

        ext = Path(canonical_path).suffix
        playlist_path = self.get_output_path(track) + ext
        if os.path.abspath(playlist_path) == os.path.abspath(canonical_path):
            return canonical_path
        if os.path.exists(playlist_path):
            return playlist_path

        os.makedirs(os.path.dirname(os.path.abspath(playlist_path)), exist_ok=True)
        try:
            os.link(canonical_path, playlist_path)
        except OSError:
            shutil.copy2(canonical_path, playlist_path)
        return playlist_path

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
                if (folder / f"{stem}{ext}").exists()
            ]
            if not existing_paths:
                return folder / stem
            if any(self._file_matches_track_identity(track, path) for path in existing_paths):
                return folder / stem
            counter = 2
            while any((folder / f"{stem} ({counter}){ext}").exists() for ext in SUPPORTED_AUDIO_EXTENSIONS):
                counter += 1
            return folder / f"{stem} ({counter})"

        candidate = folder / stem
        if not any((folder / f"{stem}{ext}").exists() for ext in SUPPORTED_AUDIO_EXTENSIONS):
            return candidate
        counter = 2
        while any((folder / f"{stem} ({counter}){ext}").exists() for ext in SUPPORTED_AUDIO_EXTENSIONS):
            counter += 1
        return folder / f"{stem} ({counter})"

    # ── State / identity helpers ──────────────────────────────────────────

    def _mark_done(self, track: TrackMetadata, path: str):
        resolved = str(Path(path).resolve())
        for key in self._track_identity_keys(track):
            self._state[f"{TRACK_KEY_PREFIX}{key}"] = resolved
            self._identity_index[key] = resolved
        self._save_state()

    def _track_identity_keys(self, track: TrackMetadata) -> list[str]:
        keys: list[str] = []
        if track.isrc:
            keys.append(f"isrc:{track.isrc.strip().lower()}")
        if track.spotify_id:
            keys.append(f"spotify:{track.spotify_id.strip()}")

        title_key = self._normalize_identity_part(track.title)
        artist_key = self._normalize_identity_part(track.primary_artist)
        album_key = self._normalize_identity_part(track.album)

        if title_key and artist_key:
            keys.append(f"title_artist:{title_key}:{artist_key}")
        if title_key and artist_key and album_key and album_key != "unknown album":
            keys.append(f"title_artist_album:{title_key}:{artist_key}:{album_key}")

        # Source-independent all-artists key: splits combined strings like
        # "Future & Metro Boomin", normalizes each part, sorts them — so
        # ["Future", "Metro Boomin"], ["Metro Boomin", "Future"], and
        # ["Future & Metro Boomin"] (single combined tag) all produce the same key.
        if track.artists:
            canonical = self._artists_canonical_key(track.artists)
            if title_key and canonical:
                keys.append(f"title_artists:{title_key}:{canonical}")

        return list(dict.fromkeys(keys))

    def _build_identity_index(self):
        for key, path in self._load_track_entries_from_state().items():
            if os.path.exists(path):
                self._identity_index.setdefault(key, path)

        for file_path in self.root.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            try:
                identity_keys = self._extract_identity_keys_from_file(file_path)
            except Exception as e:
                logger.debug(f"Library scan skipped {file_path}: {e}")
                continue
            for key in identity_keys:
                self._identity_index.setdefault(key, str(file_path.resolve()))

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
                pass
        return {}

    def _save_state(self):
        try:
            self._state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
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
        return bool(existing_keys.intersection(self._track_identity_keys(track)))

    def _extract_flac_identity_keys(self, path: Path) -> list[str]:
        audio = FLAC(path)
        title = self._first(audio.get("title"))
        artists = audio.get("artist", [])
        album = self._first(audio.get("album"))
        isrc = self._first(audio.get("isrc"))
        spotify_id = self._first(audio.get("spotify_id"))
        return self._identity_keys_from_values(title, artists, album, isrc, spotify_id)

    def _extract_mp3_identity_keys(self, path: Path) -> list[str]:
        audio = ID3(path)
        title = self._id3_text(audio.getall("TIT2"))
        artists = self._id3_text_list(audio.getall("TPE1"))
        album = self._id3_text(audio.getall("TALB"))
        isrc = self._id3_text(audio.getall("TSRC"))
        spotify_id = self._id3_txxx(audio, "SPOTIFYID")
        return self._identity_keys_from_values(title, artists, album, isrc, spotify_id)

    def _extract_mp4_identity_keys(self, path: Path) -> list[str]:
        audio = MP4(path)
        title = self._first(audio.get("\xa9nam"))
        artists = [value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
                   for value in audio.get("\xa9ART", [])]
        album = self._first(audio.get("\xa9alb"))
        isrc = self._first_freeform(audio, "ISRC")
        spotify_id = self._first_freeform(audio, "SPOTIFYID")
        return self._identity_keys_from_values(title, artists, album, isrc, spotify_id)

    def _extract_filename_identity_keys(self, path: Path) -> list[str]:
        stem = path.stem
        stem = re.sub(r"^\d+\s*-\s*", "", stem).strip()
        title = stem or "Unknown Track"
        return self._identity_keys_from_values(title, [], None, None, None)

    def _identity_keys_from_values(
        self,
        title: Optional[str],
        artists: list[str],
        album: Optional[str],
        isrc: Optional[str],
        spotify_id: Optional[str],
    ) -> list[str]:
        keys: list[str] = []
        if isrc:
            keys.append(f"isrc:{isrc.strip().lower()}")
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
