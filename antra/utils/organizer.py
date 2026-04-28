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

logger = logging.getLogger(__name__)

STATE_FILE = ".antra_state.json"
TRACK_KEY_PREFIX = "TRACK:"
FAILED_PREFIX = "FAILED:"
SUPPORTED_AUDIO_EXTENSIONS = (".flac", ".mp3", ".aac", ".m4a", ".mp4", ".opus")


class LibraryOrganizer:
    """
    Library structure:
      <root>/Albums/<Artist>/<Album (Year)>/<NN - Track Title>.<ext>
      <root>/Playlists/<Playlist Name>/<NN - Track Title>.<ext>
      <root>/Playlists/<Playlist Name>.m3u

    Deduplication is global across the library. The first downloaded file path
    becomes canonical and later playlist/album/song downloads reuse that file.
    """

    def __init__(
        self,
        root: str,
        full_albums: bool = False,
        folder_structure: str = "standard",
        filename_format: str = "default",
    ):
        self.root = Path(root).resolve()
        self.full_albums = full_albums
        self.folder_structure = folder_structure
        self.filename_format = filename_format
        self.root.mkdir(parents=True, exist_ok=True)
        self.albums_root = self.root / "Albums"
        self.playlists_root = self.root / "Playlists"
        self.albums_root.mkdir(parents=True, exist_ok=True)
        self.playlists_root.mkdir(parents=True, exist_ok=True)
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
            filename = self._format_filename(track, track_number, is_playlist=True)
            if self.folder_structure == "flat":
                folder = self.root / playlist_dir
            else:
                folder = self.playlists_root / playlist_dir
            folder.mkdir(parents=True, exist_ok=True)
            return str(folder / filename)

        # Use album-level artists for the folder name so joint albums
        # (e.g. "PARTYNEXTDOOR & Drake") land in one combined folder
        # instead of splitting by per-track artist.
        if track.album_artists:
            artist_dir = self._safe(", ".join(track.album_artists))
        else:
            artist_dir = self._safe(", ".join(track.artists))
        album_part = self._safe(track.album)
        if track.release_year:
            album_dir = f"{album_part} ({track.release_year})"
        else:
            album_dir = album_part

        filename = self._format_filename(track, track.track_number, is_playlist=False)

        if self.folder_structure == "flat":
            folder = self.root / album_dir
        else:
            folder = self.albums_root / artist_dir / album_dir
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / filename)

    def _format_filename(self, track: TrackMetadata, track_number: Optional[int], is_playlist: bool = False) -> str:
        """Build the filename stem according to self.filename_format."""
        title = self._safe(track.title)
        artist = self._safe(track.primary_artist)

        # Disc number: playlists always use disc=1 (no multi-disc concept),
        # albums use the actual disc number or default to 1.
        # This produces 101/102/201/202 for albums and 101/102/... for playlists.
        disc = track.disc_number or 1

        # title_only keeps its original behaviour (no number prefix at all,
        # except when the album is explicitly multi-disc to avoid collisions).
        if self.filename_format == "title_only":
            is_multi_disc = (
                (track.total_discs is not None and track.total_discs > 1)
                or (track.disc_number is not None and track.disc_number >= 2)
            )
            if is_multi_disc and track.disc_number and track_number:
                return f"{track.disc_number}{track_number:02d} - {title}"
            return title

        if self.filename_format == "artist_title":
            if track_number:
                return f"{disc}{track_number:02d} - {artist} - {title}"
            return f"{artist} - {title}"

        if self.filename_format == "title_artist":
            if track_number:
                return f"{disc}{track_number:02d} - {title} - {artist}"
            return f"{title} - {artist}"

        # default: disc-prefixed numbering (101, 102, 201, 202) for both
        # albums and playlists. Playlists always use disc=1 → 101, 102, ...
        if track_number:
            return f"{disc}{track_number:02d} - {title}"
        return title

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
        if not self.full_albums:
            for key in self._track_identity_keys(track):
                existing = self._identity_index.get(key)
                if existing and os.path.exists(existing):
                    return existing

        # Check the expected canonical path for this exact request.
        base = self.get_output_path(track)
        for ext in SUPPORTED_AUDIO_EXTENSIONS:
            candidate = base + ext
            if os.path.exists(candidate):
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
        manifest_root = self.root if self.folder_structure == "flat" else self.playlists_root
        manifest_path = manifest_root / f"{self._safe(playlist_name)}.m3u"
        lines = ["#EXTM3U"]
        for file_path in file_paths:
            if not file_path:
                continue
            relative = os.path.relpath(file_path, manifest_path.parent)
            lines.append(Path(relative).as_posix())
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(manifest_path)

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
