"""
Library maintenance helpers for album duplicate detection and consolidation.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

SUPPORTED_AUDIO_EXTENSIONS = (".flac", ".mp3", ".aac", ".m4a", ".mp4", ".opus")
SIDECAR_EXTENSIONS = (".lrc", ".txt")


@dataclass
class TrackEntry:
    path: Path
    identity: str
    identity_keys: list[str]
    title: str
    artist: str
    album: str


@dataclass
class AlbumEntry:
    path: Path
    artist: str
    album: str
    tracks: list[TrackEntry]


@dataclass
class DuplicateAlbumGroup:
    artist: str
    album: str
    canonical: Path
    duplicates: list[Path]


@dataclass
class DuplicateSongGroup:
    identity: str
    title: str
    artist: str
    canonical: Path
    duplicates: list[Path]


@dataclass
class DedupeReport:
    groups_found: int = 0
    duplicate_albums_removed: int = 0
    tracks_moved: int = 0
    duplicate_tracks_deleted: int = 0


@dataclass
class SongDedupeReport:
    groups_found: int = 0
    duplicate_tracks_deleted: int = 0
    playlist_entries_rewritten: int = 0


def find_duplicate_albums(root: str) -> list[DuplicateAlbumGroup]:
    album_entries = _scan_album_entries(Path(root).resolve())
    grouped: dict[str, list[AlbumEntry]] = {}
    for entry in album_entries:
        grouped.setdefault(_album_key(entry.artist, entry.album), []).append(entry)

    duplicates: list[DuplicateAlbumGroup] = []
    for entries in grouped.values():
        if len(entries) < 2:
            continue
        canonical = _choose_canonical_album(entries)
        duplicate_paths = [entry.path for entry in entries if entry.path != canonical.path]
        duplicates.append(
            DuplicateAlbumGroup(
                artist=canonical.artist,
                album=canonical.album,
                canonical=canonical.path,
                duplicates=duplicate_paths,
            )
        )

    duplicates.sort(key=lambda group: (group.artist.lower(), group.album.lower(), str(group.canonical).lower()))
    return duplicates


def dedupe_duplicate_albums(root: str) -> DedupeReport:
    album_entries = _scan_album_entries(Path(root).resolve())
    by_path = {entry.path: entry for entry in album_entries}
    report = DedupeReport()

    for group in find_duplicate_albums(root):
        report.groups_found += 1
        canonical_entry = by_path[group.canonical]
        canonical_tracks = {track.identity: track for track in canonical_entry.tracks}

        for duplicate_path in group.duplicates:
            duplicate_entry = by_path[duplicate_path]
            for track in duplicate_entry.tracks:
                if track.identity in canonical_tracks:
                    _delete_track_bundle(track.path)
                    report.duplicate_tracks_deleted += 1
                    continue

                target_path = _resolve_move_target(canonical_entry.path, track.path.name)
                shutil.move(str(track.path), str(target_path))
                _move_sidecars(track.path, target_path)
                canonical_tracks[track.identity] = TrackEntry(
                    path=target_path,
                    identity=track.identity,
                    identity_keys=track.identity_keys,
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                )
                report.tracks_moved += 1

            _remove_empty_dir(duplicate_path)
            if not duplicate_path.exists():
                report.duplicate_albums_removed += 1

    return report


def find_duplicate_songs(root: str) -> list[DuplicateSongGroup]:
    root_path = Path(root).resolve()
    tracks = _scan_track_entries(root_path)
    groups_by_id: dict[int, list[TrackEntry]] = {}
    key_to_group: dict[str, int] = {}
    next_group_id = 0

    for track in tracks:
        matching_group_ids = {key_to_group[key] for key in track.identity_keys if key in key_to_group}
        if not matching_group_ids:
            group_id = next_group_id
            next_group_id += 1
            groups_by_id[group_id] = [track]
            for key in track.identity_keys:
                key_to_group[key] = group_id
            continue

        group_id = min(matching_group_ids)
        groups_by_id.setdefault(group_id, []).append(track)
        for other_group_id in sorted(matching_group_ids - {group_id}):
            for merged_track in groups_by_id.pop(other_group_id, []):
                groups_by_id[group_id].append(merged_track)
                for key in merged_track.identity_keys:
                    key_to_group[key] = group_id
        for key in track.identity_keys:
            key_to_group[key] = group_id

    duplicates: list[DuplicateSongGroup] = []
    for entries in groups_by_id.values():
        if len(entries) < 2:
            continue
        canonical = _choose_canonical_track(entries)
        duplicate_paths = [entry.path for entry in entries if entry.path != canonical.path]
        duplicates.append(
            DuplicateSongGroup(
                identity=canonical.identity,
                title=canonical.title,
                artist=canonical.artist,
                canonical=canonical.path,
                duplicates=duplicate_paths,
            )
        )

    duplicates.sort(key=lambda group: (group.artist.lower(), group.title.lower(), str(group.canonical).lower()))
    return duplicates


def dedupe_duplicate_songs(root: str) -> SongDedupeReport:
    root_path = Path(root).resolve()
    report = SongDedupeReport()

    for group in find_duplicate_songs(str(root_path)):
        report.groups_found += 1
        for duplicate_path in group.duplicates:
            report.playlist_entries_rewritten += _rewrite_playlist_manifests(root_path, duplicate_path, group.canonical)
            _delete_track_bundle(duplicate_path)
            report.duplicate_tracks_deleted += 1

    return report


def _scan_album_entries(root: Path) -> list[AlbumEntry]:
    albums_root = root / "Albums"
    if not albums_root.exists():
        return []

    entries: list[AlbumEntry] = []
    for directory in albums_root.rglob("*"):
        if not directory.is_dir():
            continue
        audio_files = [
            child for child in sorted(directory.iterdir())
            if child.is_file() and child.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        tracks = [_read_track_entry(path) for path in audio_files]
        album_name = _majority([track.album for track in tracks]) or _strip_year_suffix(directory.name)
        artist_name = _majority([track.artist for track in tracks]) or directory.parent.name
        entries.append(AlbumEntry(path=directory, artist=artist_name, album=album_name, tracks=tracks))
    return entries


def _scan_track_entries(root: Path) -> list[TrackEntry]:
    tracks: list[TrackEntry] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        tracks.append(_read_track_entry(path))
    return tracks


def _read_track_entry(path: Path) -> TrackEntry:
    title, artist, album, isrc, spotify_id = _read_tag_fields(path)
    title = title or _filename_title(path)
    if not artist and _is_under_albums(path):
        artist = path.parent.parent.name
    if not album and _is_under_albums(path):
        album = _strip_year_suffix(path.parent.name)
    artist = artist or ""
    album = album or ""
    identity_keys = _track_identity_keys(title, artist, album, isrc, spotify_id)
    identity = identity_keys[0]
    return TrackEntry(path=path, identity=identity, identity_keys=identity_keys, title=title, artist=artist, album=album)


def _read_tag_fields(path: Path) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(path)
            return (
                _first(audio.get("title")),
                _first(audio.get("artist")),
                _first(audio.get("album")),
                _first(audio.get("isrc")),
                _first(audio.get("spotify_id")),
            )
        if ext == ".mp3":
            audio = ID3(path)
            return (
                _id3_text(audio, "TIT2"),
                _id3_text(audio, "TPE1"),
                _id3_text(audio, "TALB"),
                _id3_text(audio, "TSRC"),
                _id3_txxx(audio, "SPOTIFYID"),
            )
        if ext in {".m4a", ".mp4"}:
            audio = MP4(path)
            return (
                _first(audio.get("\xa9nam")),
                _first(audio.get("\xa9ART")),
                _first(audio.get("\xa9alb")),
                _first_freeform(audio, "ISRC"),
                _first_freeform(audio, "SPOTIFYID"),
            )
    except Exception:
        return None, None, None, None, None
    return None, None, None, None, None


def _track_identity_keys(
    title: str,
    artist: str,
    album: str,
    isrc: Optional[str],
    spotify_id: Optional[str],
) -> list[str]:
    keys: list[str] = []
    if isrc:
        keys.append(f"isrc:{isrc.strip().lower()}")
    if spotify_id:
        keys.append(f"spotify:{spotify_id.strip()}")
    title_key = _normalize(title)
    artist_key = _normalize(artist)
    album_key = _normalize(album)
    if title_key:
        keys.append(f"title:{title_key}")
    if title_key and artist_key:
        keys.append(f"title_artist:{title_key}:{artist_key}")
    if title_key and artist_key and album_key:
        keys.append(f"title_artist_album:{title_key}:{artist_key}:{album_key}")
    return list(dict.fromkeys(keys))


def _album_key(artist: str, album: str) -> str:
    return f"{_normalize(artist)}::{_normalize(_strip_year_suffix(album))}"


def _choose_canonical_album(entries: list[AlbumEntry]) -> AlbumEntry:
    return sorted(
        entries,
        key=lambda entry: (
            not _has_year_suffix(entry.path.name),
            -len({track.identity for track in entry.tracks}),
            len(entry.path.parts),
            str(entry.path).lower(),
        ),
    )[0]


def _choose_canonical_track(entries: list[TrackEntry]) -> TrackEntry:
    return sorted(
        entries,
        key=lambda entry: (
            not _is_under_albums(entry.path),
            entry.path.stat().st_mtime,
            len(entry.path.parts),
            str(entry.path).lower(),
        ),
    )[0]


def _resolve_move_target(canonical_dir: Path, original_name: str) -> Path:
    target = canonical_dir / original_name
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    counter = 2
    while True:
        candidate = canonical_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _move_sidecars(source_audio: Path, target_audio: Path):
    for ext in SIDECAR_EXTENSIONS:
        source = source_audio.with_suffix(ext)
        if source.exists():
            shutil.move(str(source), str(target_audio.with_suffix(ext)))


def _rewrite_playlist_manifests(root: Path, duplicate_audio: Path, canonical_audio: Path) -> int:
    rewrites = 0
    for manifest in root.rglob("*.m3u"):
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        changed = False
        new_lines: list[str] = []
        duplicate_abs = duplicate_audio.resolve()
        canonical_rel = Path(os.path.relpath(canonical_audio, manifest.parent)).as_posix()

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            referenced = (manifest.parent / Path(stripped)).resolve()
            if referenced == duplicate_abs:
                new_lines.append(canonical_rel)
                changed = True
                rewrites += 1
            else:
                new_lines.append(line)

        if changed:
            manifest.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return rewrites


def _delete_track_bundle(audio_path: Path):
    if audio_path.exists():
        audio_path.unlink()
    for ext in SIDECAR_EXTENSIONS:
        sidecar = audio_path.with_suffix(ext)
        if sidecar.exists():
            sidecar.unlink()
    _remove_empty_dir(audio_path.parent)


def _remove_empty_dir(path: Path):
    current = path
    while current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _filename_title(path: Path) -> str:
    stem = re.sub(r"^\d+\s*-\s*", "", path.stem).strip()
    return stem or path.stem


def _strip_year_suffix(name: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip() or name


def _has_year_suffix(name: str) -> bool:
    return bool(re.search(r"\(\d{4}\)\s*$", name))


def _is_under_albums(path: Path) -> bool:
    return "Albums" in path.parts


def _normalize(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return value.strip()


def _majority(values: list[str]) -> Optional[str]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[0][0]


def _first(values) -> Optional[str]:
    if not values:
        return None
    value = values[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _id3_text(audio: ID3, key: str) -> Optional[str]:
    frames = audio.getall(key)
    if not frames:
        return None
    texts = getattr(frames[0], "text", None) or []
    return str(texts[0]) if texts else None


def _id3_txxx(audio: ID3, desc: str) -> Optional[str]:
    for frame in audio.getall("TXXX"):
        if getattr(frame, "desc", "") == desc:
            texts = getattr(frame, "text", None) or []
            return str(texts[0]) if texts else None
    return None


def _first_freeform(audio: MP4, desc: str) -> Optional[str]:
    key = f"----:com.apple.iTunes:{desc}"
    values = audio.get(key, [])
    if not values:
        return None
    value = values[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)
