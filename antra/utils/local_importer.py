"""
Import local audio files into the configured Antra library layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import logging
import os
import re
import shutil
import wave
from typing import Callable, Optional

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from antra.core.events import EngineEvent, EngineEventType
from antra.core.models import AudioFormat, DownloadResult, DownloadStatus, TrackMetadata
from antra.utils.lyrics import LyricsFetcher
from antra.utils.organizer import LibraryOrganizer, SUPPORTED_AUDIO_EXTENSIONS
from antra.utils.tagger import FileTagger

logger = logging.getLogger(__name__)

SIDECAR_EXTENSIONS = (".lrc", ".txt")
LOSSLESS_EXTENSIONS = {".flac", ".alac", ".wav", ".wave", ".aiff", ".aif"}
LOSSY_EXTENSIONS = {".mp3", ".aac", ".m4a", ".mp4", ".opus", ".ogg"}


@dataclass(frozen=True)
class AudioQuality:
    extension: str
    lossless_rank: int
    bits_per_sample: int
    sample_rate: int
    bitrate: int

    @property
    def score(self) -> tuple[int, int, int, int]:
        return (
            self.lossless_rank,
            self.bits_per_sample,
            self.sample_rate,
            self.bitrate if self.lossless_rank == 0 else 0,
        )


@dataclass
class LocalImportSummary:
    total: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    total_bytes: int = 0
    results: list[DownloadResult] = field(default_factory=list)


class LocalMusicImporter:
    def __init__(
        self,
        organizer: LibraryOrganizer,
        event_callback: Optional[Callable[[EngineEvent], None]] = None,
        copy_sidecars: bool = True,
        lyrics_fetcher: Optional[LyricsFetcher] = None,
        tag_imports: bool = True,
    ):
        self.organizer = organizer
        self.event_callback = event_callback
        self.copy_sidecars = copy_sidecars
        self.lyrics_fetcher = lyrics_fetcher
        self.tagger = FileTagger() if tag_imports else None

    def import_files(
        self,
        files: list[Path],
        *,
        tracks: Optional[list[TrackMetadata]] = None,
        parse_errors: Optional[list[Optional[str]]] = None,
    ) -> LocalImportSummary:
        summary = LocalImportSummary(total=len(files))

        for index, path in enumerate(files, start=1):
            track = tracks[index - 1] if tracks else None
            parse_error = parse_errors[index - 1] if parse_errors else None
            result = self.import_file(
                path,
                track_index=index,
                track_total=len(files),
                track=track,
                parse_error=parse_error,
            )
            summary.results.append(result)
            if result.status == DownloadStatus.COMPLETED:
                summary.imported += 1
                if result.file_path and os.path.exists(result.file_path):
                    summary.total_bytes += os.path.getsize(result.file_path)
            elif result.status == DownloadStatus.SKIPPED:
                summary.skipped += 1
            elif result.status == DownloadStatus.FAILED:
                summary.failed += 1

        return summary

    def import_file(
        self,
        path: Path,
        track_index: Optional[int] = None,
        track_total: Optional[int] = None,
        track: Optional[TrackMetadata] = None,
        parse_error: Optional[str] = None,
    ) -> DownloadResult:
        if track is None:
            try:
                track = track_metadata_from_file(path)
            except Exception as exc:
                track = fallback_track_metadata(path)
                parse_error = str(exc)

        self._sanitize_metadata(track)

        if parse_error:
            self._emit(
                EngineEventType.TRACK_FAILED,
                track=track,
                track_index=track_index,
                track_total=track_total,
                source="local",
                error=parse_error,
            )
            return DownloadResult(
                track=track,
                status=DownloadStatus.FAILED,
                file_path=str(path),
                source_used="local",
                error_message=parse_error,
            )

        self._emit(
            EngineEventType.TRACK_STARTED,
            track=track,
            track_index=track_index,
            track_total=track_total,
            source="local",
            message=str(path),
        )

        existing = self.organizer.is_already_downloaded(track)
        replacing_existing = False
        if existing:
            if should_replace_existing(path, existing):
                replacing_existing = True
                destination = replacement_destination(existing, path)
            else:
                self.organizer.mark_downloaded(track, existing)
                self._emit(
                    EngineEventType.TRACK_SKIPPED,
                    track=track,
                    track_index=track_index,
                    track_total=track_total,
                    source="local",
                    file_path=existing,
                    message="Track already exists on disk.",
                )
                return DownloadResult(
                    track=track,
                    status=DownloadStatus.SKIPPED,
                    file_path=existing,
                    source_used="local",
                    audio_format=audio_format_from_path(existing),
                )

        if not replacing_existing:
            destination = Path(self.organizer.get_output_path(track) + path.suffix.lower())

        if replacing_existing:
            existing_path = Path(existing)
            resolved_message = (
                "Replacing lower-quality library file "
                f"({quality_label_from_file(existing_path)} -> {quality_label_from_file(path)})"
            )
        else:
            existing_path = None
            resolved_message = "Importing local file"

        self._emit(
            EngineEventType.TRACK_RESOLVED,
            track=track,
            track_index=track_index,
            track_total=track_total,
            source="local",
            quality_label=quality_label_from_file(path),
            message=resolved_message,
        )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.resolve() != destination.resolve():
                shutil.copy2(path, destination)
                if self.copy_sidecars:
                    copy_sidecars(path, destination)
            self._fetch_lyrics_if_needed(track)
            if self.tagger:
                self.tagger.tag(str(destination), track)
            if existing_path and existing_path.resolve() != destination.resolve() and existing_path.exists():
                existing_path.unlink()
            self.organizer.mark_downloaded(track, str(destination))
        except Exception as exc:
            self._emit(
                EngineEventType.TRACK_FAILED,
                track=track,
                track_index=track_index,
                track_total=track_total,
                source="local",
                error=str(exc),
            )
            return DownloadResult(
                track=track,
                status=DownloadStatus.FAILED,
                file_path=str(path),
                source_used="local",
                error_message=str(exc),
            )

        self._emit(
            EngineEventType.TRACK_COMPLETED,
            track=track,
            track_index=track_index,
            track_total=track_total,
            source="local",
            file_path=str(destination),
            quality_label=quality_label_from_file(destination),
        )
        return DownloadResult(
            track=track,
            status=DownloadStatus.COMPLETED,
            file_path=str(destination),
            source_used="local",
            audio_format=audio_format_from_path(str(destination)),
        )

    def _emit(self, event_type: EngineEventType, **kwargs) -> None:
        if self.event_callback:
            self.event_callback(EngineEvent(type=event_type, **kwargs))

    def _sanitize_metadata(self, track: TrackMetadata) -> None:
        normalized_isrc = self.organizer._normalize_isrc(track.isrc)
        track.isrc = normalized_isrc.upper() if normalized_isrc else None

    def _fetch_lyrics_if_needed(self, track: TrackMetadata) -> None:
        if not self.lyrics_fetcher:
            return
        if track.lyrics or track.synced_lyrics:
            return
        try:
            lyrics, synced_lyrics = self.lyrics_fetcher.fetch(track)
        except Exception as exc:
            logger.debug("Local import lyrics lookup failed for %s: %s", track.title, exc)
            return
        track.lyrics = lyrics
        track.synced_lyrics = synced_lyrics

    @staticmethod
    def discover_audio_files(paths: list[str]) -> list[Path]:
        files: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                iterator = candidate.rglob("*")
            else:
                iterator = [candidate]
            for path in iterator:
                try:
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                        continue
                    resolved = str(path.resolve())
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                files.append(path.resolve())
        return sorted(files, key=lambda p: str(p).lower())


def track_metadata_from_file(path: Path) -> TrackMetadata:
    ext = path.suffix.lower()
    if ext == ".flac":
        return _metadata_from_flac(path)
    if ext == ".mp3":
        return _metadata_from_mp3(path)
    if ext in {".m4a", ".mp4", ".alac"}:
        return _metadata_from_mp4(path)
    return _metadata_from_generic(path)


def fallback_track_metadata(path: Path) -> TrackMetadata:
    return _metadata_from_values(
        path=path,
        title=None,
        artists=[],
        album=None,
        album_artists=[],
        release_date=None,
        track_number=None,
        disc_number=None,
        total_tracks=None,
        total_discs=None,
        duration_ms=None,
        isrc=None,
        spotify_id=None,
        genres=[],
    )


def _metadata_from_flac(path: Path) -> TrackMetadata:
    audio = FLAC(path)
    return _metadata_from_values(
        path=path,
        title=_first(audio.get("title")),
        artists=_list_values(audio.get("artist")),
        album=_first(audio.get("album")),
        album_artists=_list_values(audio.get("albumartist") or audio.get("album_artist")),
        release_date=_first(audio.get("date") or audio.get("originaldate") or audio.get("year")),
        track_number=_number_pair_first(_first(audio.get("tracknumber"))),
        disc_number=_number_pair_first(_first(audio.get("discnumber"))),
        total_tracks=_number_pair_second(_first(audio.get("tracknumber"))) or _int_or_none(_first(audio.get("tracktotal"))),
        total_discs=_number_pair_second(_first(audio.get("discnumber"))) or _int_or_none(_first(audio.get("disctotal"))),
        duration_ms=_duration_ms(path),
        isrc=_first(audio.get("isrc")),
        spotify_id=_first(audio.get("spotify_id")),
        genres=_list_values(audio.get("genre")),
    )


def _metadata_from_mp3(path: Path) -> TrackMetadata:
    audio = ID3(path)
    date = _id3_text(audio.getall("TDRC")) or _id3_text(audio.getall("TYER"))
    track_text = _id3_text(audio.getall("TRCK"))
    disc_text = _id3_text(audio.getall("TPOS"))
    return _metadata_from_values(
        path=path,
        title=_id3_text(audio.getall("TIT2")),
        artists=_id3_text_list(audio.getall("TPE1")),
        album=_id3_text(audio.getall("TALB")),
        album_artists=_id3_text_list(audio.getall("TPE2")),
        release_date=date,
        track_number=_number_pair_first(track_text),
        disc_number=_number_pair_first(disc_text),
        total_tracks=_number_pair_second(track_text),
        total_discs=_number_pair_second(disc_text),
        duration_ms=_duration_ms(path),
        isrc=_id3_text(audio.getall("TSRC")),
        spotify_id=_id3_txxx(audio, "SPOTIFYID"),
        genres=_id3_text_list(audio.getall("TCON")),
    )


def _metadata_from_mp4(path: Path) -> TrackMetadata:
    audio = MP4(path)
    track_pair = _mp4_pair(audio.get("trkn"))
    disc_pair = _mp4_pair(audio.get("disk"))
    return _metadata_from_values(
        path=path,
        title=_first(audio.get("\xa9nam")),
        artists=_list_values(audio.get("\xa9ART")),
        album=_first(audio.get("\xa9alb")),
        album_artists=_list_values(audio.get("aART")),
        release_date=_first(audio.get("\xa9day")),
        track_number=track_pair[0],
        disc_number=disc_pair[0],
        total_tracks=track_pair[1],
        total_discs=disc_pair[1],
        duration_ms=_duration_ms(path),
        isrc=_first_freeform(audio, "ISRC"),
        spotify_id=_first_freeform(audio, "SPOTIFYID"),
        genres=_list_values(audio.get("\xa9gen")),
    )


def _metadata_from_generic(path: Path) -> TrackMetadata:
    audio = MutagenFile(path, easy=True)
    tags = getattr(audio, "tags", {}) if audio else {}
    return _metadata_from_values(
        path=path,
        title=_tag_first(tags, "title"),
        artists=_tag_list(tags, "artist"),
        album=_tag_first(tags, "album"),
        album_artists=_tag_list(tags, "albumartist") or _tag_list(tags, "album_artist"),
        release_date=_tag_first(tags, "date") or _tag_first(tags, "year"),
        track_number=_number_pair_first(_tag_first(tags, "tracknumber")),
        disc_number=_number_pair_first(_tag_first(tags, "discnumber")),
        total_tracks=_number_pair_second(_tag_first(tags, "tracknumber")) or _int_or_none(_tag_first(tags, "tracktotal")),
        total_discs=_number_pair_second(_tag_first(tags, "discnumber")) or _int_or_none(_tag_first(tags, "disctotal")),
        duration_ms=_duration_ms(path),
        isrc=_tag_first(tags, "isrc"),
        spotify_id=_tag_first(tags, "spotify_id"),
        genres=_tag_list(tags, "genre"),
    )


def _metadata_from_values(
    *,
    path: Path,
    title: Optional[str],
    artists: list[str],
    album: Optional[str],
    album_artists: list[str],
    release_date: Optional[str],
    track_number: Optional[int],
    disc_number: Optional[int],
    total_tracks: Optional[int],
    total_discs: Optional[int],
    duration_ms: Optional[int],
    isrc: Optional[str],
    spotify_id: Optional[str],
    genres: list[str],
) -> TrackMetadata:
    inferred_title, inferred_artist, inferred_album, inferred_track = infer_metadata_from_path(path)
    clean_title = clean_tag_value(title) or inferred_title
    clean_artists = clean_tag_list(artists)
    if not clean_artists and inferred_artist:
        clean_artists = [inferred_artist]
    clean_album = clean_tag_value(album) or inferred_album or "Unknown Album"
    clean_album_artists = clean_tag_list(album_artists)
    if not clean_album_artists and clean_artists:
        clean_album_artists = [clean_artists[0]]

    release_year = release_year_from_date(release_date)
    return TrackMetadata(
        title=clean_title or "Unknown Track",
        artists=clean_artists or ["Unknown Artist"],
        album=clean_album,
        release_year=release_year,
        release_date=clean_tag_value(release_date),
        track_number=track_number or inferred_track,
        disc_number=disc_number,
        total_tracks=total_tracks,
        total_discs=total_discs,
        duration_ms=duration_ms,
        isrc=clean_tag_value(isrc),
        spotify_id=clean_tag_value(spotify_id),
        genres=clean_tag_list(genres),
        album_artists=clean_album_artists,
    )


def infer_metadata_from_path(path: Path) -> tuple[str, Optional[str], Optional[str], Optional[int]]:
    stem = path.stem.strip()
    track_number = None
    match = re.match(r"^\s*(?:disc\s*)?(\d{1,3})(?:[._ -]+)", stem, flags=re.IGNORECASE)
    if match:
        raw_number = int(match.group(1))
        track_number = raw_number % 100 if raw_number >= 100 else raw_number
        stem = stem[match.end():].strip()

    artist = None
    title = stem or "Unknown Track"
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem) if part.strip()]
    if len(parts) >= 2:
        artist = parts[0]
        title = " - ".join(parts[1:])

    album = path.parent.name if path.parent and path.parent.name else None
    if album and album.lower() in {"music", "downloads", "desktop"}:
        album = None
    if artist is None and path.parent and path.parent.parent and album:
        parent_artist = path.parent.parent.name
        if parent_artist and parent_artist.lower() not in {"music", "downloads", "desktop"}:
            artist = parent_artist

    return title or "Unknown Track", artist, album, track_number


def copy_sidecars(source: Path, destination: Path) -> None:
    for ext in SIDECAR_EXTENSIONS:
        sidecar = source.with_suffix(ext)
        if not sidecar.exists() or not sidecar.is_file():
            continue
        shutil.copy2(sidecar, destination.with_suffix(ext))


def audio_format_from_path(path: str) -> Optional[AudioFormat]:
    ext = Path(path).suffix.lower()
    return {
        ".flac": AudioFormat.FLAC,
        ".alac": AudioFormat.ALAC,
        ".mp3": AudioFormat.MP3,
        ".aac": AudioFormat.AAC,
        ".m4a": AudioFormat.AAC,
        ".mp4": AudioFormat.AAC,
        ".opus": AudioFormat.OPUS,
    }.get(ext)


def quality_label_from_file(path: Path) -> str:
    ext_label = path.suffix.lower().lstrip(".").upper() or "AUDIO"
    try:
        audio = MutagenFile(path)
        info = getattr(audio, "info", None)
        if not info:
            return ext_label
        bits = getattr(info, "bits_per_sample", None)
        rate = getattr(info, "sample_rate", None)
        bitrate = getattr(info, "bitrate", None)
        if bits and rate:
            return f"{ext_label} {bits}-bit/{int(rate) // 1000}kHz"
        if bitrate:
            return f"{ext_label} {int(bitrate) // 1000}kbps"
    except Exception:
        pass
    return ext_label


def should_replace_existing(candidate_path: Path, existing_path: str) -> bool:
    candidate_quality = probe_audio_quality(candidate_path)
    existing_quality = probe_audio_quality(Path(existing_path))
    if not candidate_quality or not existing_quality:
        return False
    return candidate_quality.score > existing_quality.score


def replacement_destination(existing_path: str, candidate_path: Path) -> Path:
    existing = Path(existing_path)
    candidate_ext = candidate_path.suffix.lower()
    if candidate_ext and candidate_ext != existing.suffix.lower():
        return existing.with_suffix(candidate_ext)
    return existing


def probe_audio_quality(path: Path) -> Optional[AudioQuality]:
    try:
        audio = MutagenFile(path)
    except Exception:
        audio = None
    info = getattr(audio, "info", None) if audio else None
    if not info:
        return probe_pcm_quality(path)

    ext = path.suffix.lower()
    codec = str(getattr(info, "codec", "") or "").lower()
    lossless_rank = 1 if ext in LOSSLESS_EXTENSIONS or codec.startswith("alac") else 0
    if ext in LOSSY_EXTENSIONS and not codec.startswith("alac"):
        lossless_rank = 0

    bits = _int_or_none(getattr(info, "bits_per_sample", None)) or 0
    sample_width = _int_or_none(getattr(info, "sample_width", None))
    if not bits and sample_width:
        bits = sample_width * 8

    return AudioQuality(
        extension=ext,
        lossless_rank=lossless_rank,
        bits_per_sample=bits,
        sample_rate=_int_or_none(getattr(info, "sample_rate", None)) or 0,
        bitrate=_int_or_none(getattr(info, "bitrate", None)) or 0,
    )


def probe_pcm_quality(path: Path) -> Optional[AudioQuality]:
    ext = path.suffix.lower()
    if ext not in {".wav", ".wave"}:
        return None
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            bits = audio.getsampwidth() * 8
            sample_rate = audio.getframerate()
    except Exception:
        return None

    return AudioQuality(
        extension=ext,
        lossless_rank=1,
        bits_per_sample=bits,
        sample_rate=sample_rate,
        bitrate=bits * sample_rate * channels,
    )


def _duration_ms(path: Path) -> Optional[int]:
    try:
        audio = MutagenFile(path)
        info = getattr(audio, "info", None) if audio else None
        length = getattr(info, "length", None) if info else None
        return int(float(length) * 1000) if length else None
    except Exception:
        return None


def release_year_from_date(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"\b(\d{4})\b", str(value))
    return int(match.group(1)) if match else None


def clean_tag_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def clean_tag_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        if value is None:
            continue
        stripped = str(value).strip()
        parts = [p.strip() for p in stripped.split(";")] if ";" in stripped else [stripped]
        for part in parts:
            if part and part not in cleaned:
                cleaned.append(part)
    return cleaned


def _first(values) -> Optional[str]:
    if not values:
        return None
    value = values[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _list_values(values) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, bytes):
            result.append(value.decode("utf-8", errors="ignore"))
        else:
            result.append(str(value))
    return result


def _tag_first(tags, key: str) -> Optional[str]:
    try:
        return _first(tags.get(key))
    except Exception:
        return None


def _tag_list(tags, key: str) -> list[str]:
    try:
        return _list_values(tags.get(key))
    except Exception:
        return []


def _id3_text(frames) -> Optional[str]:
    if not frames:
        return None
    texts = getattr(frames[0], "text", None) or []
    return str(texts[0]) if texts else None


def _id3_text_list(frames) -> list[str]:
    if not frames:
        return []
    texts = getattr(frames[0], "text", None) or []
    return [str(text) for text in texts if text]


def _id3_txxx(audio: ID3, desc: str) -> Optional[str]:
    for frame in audio.getall("TXXX"):
        if getattr(frame, "desc", "") == desc:
            texts = getattr(frame, "text", None) or []
            return str(texts[0]) if texts else None
    return None


def _first_freeform(audio: MP4, desc: str) -> Optional[str]:
    key = f"----:com.apple.iTunes:{desc}"
    values = audio.get(key, [])
    return _first(values)


def _mp4_pair(values) -> tuple[Optional[int], Optional[int]]:
    if not values:
        return None, None
    first = values[0]
    if isinstance(first, tuple):
        return _int_or_none(first[0]), _int_or_none(first[1])
    return None, None


def _number_pair_first(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _number_pair_second(value: Optional[str]) -> Optional[int]:
    if not value or "/" not in str(value):
        return None
    return _number_pair_first(str(value).split("/", 1)[1])


def _int_or_none(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
