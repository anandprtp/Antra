"""
Core data models for Antra.
"""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class AudioFormat(Enum):
    FLAC = "flac"
    ALAC = "alac"
    MP3 = "mp3"
    AAC = "aac"
    OPUS = "opus"


class DownloadStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TrackMetadata:
    """Normalized track metadata from Spotify."""
    title: str
    artists: list[str]
    album: str
    playlist_name: Optional[str] = None
    playlist_position: Optional[int] = None
    release_year: Optional[int] = None
    release_date: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    total_tracks: Optional[int] = None
    duration_ms: Optional[int] = None
    isrc: Optional[str] = None
    spotify_id: Optional[str] = None
    album_id: Optional[str] = None
    spotify_url: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    artwork_url: Optional[str] = None  # Highest res from Spotify
    lyrics: Optional[str] = None
    synced_lyrics: Optional[str] = None  # LRC format

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else "Unknown Artist"

    @property
    def artist_string(self) -> str:
        return ", ".join(self.artists)

    @property
    def duration_seconds(self) -> Optional[float]:
        return self.duration_ms / 1000 if self.duration_ms else None


@dataclass
class SearchResult:
    """Result from a source adapter search."""
    source: str
    title: str
    artists: list[str]
    album: Optional[str]
    duration_ms: Optional[int]
    audio_format: AudioFormat
    quality_kbps: Optional[int]  # None for lossless
    is_lossless: bool
    download_url: Optional[str]  # Direct URL or None
    stream_id: Optional[str]     # Source-specific ID for download
    similarity_score: float = 0.0
    isrc_match: bool = False
    artwork_url: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate_hz: Optional[int] = None

    @property
    def quality_label(self) -> str:
        fmt = self.audio_format.value.upper()
        if self.is_lossless:
            if self.bit_depth and self.sample_rate_hz:
                return f"{fmt} {self.bit_depth}-bit/{self.sample_rate_hz // 1000}kHz"
            if self.bit_depth:
                return f"{fmt} {self.bit_depth}-bit"
            return fmt
        return f"{fmt} {self.quality_kbps}kbps"


@dataclass
class DownloadResult:
    """Outcome of a download attempt."""
    track: TrackMetadata
    status: DownloadStatus
    file_path: Optional[str] = None
    source_used: Optional[str] = None
    audio_format: Optional[AudioFormat] = None
    error_message: Optional[str] = None
    attempt_count: int = 1


@dataclass
class SpotifyPlaylistSummary:
    """Spotify playlist or collection entry available to the current user."""
    id: str
    name: str
    owner: str
    total_tracks: int
    description: str = ""
    url: Optional[str] = None
    kind: str = "playlist"
    is_public: Optional[bool] = None
    is_collaborative: bool = False

    @property
    def selection_key(self) -> str:
        return self.id if self.kind == "playlist" else f"{self.kind}:{self.id}"


@dataclass
class SpotifyLibrary:
    """Current user library overview for playlist selection flows."""
    user_id: str
    display_name: str
    playlists: list[SpotifyPlaylistSummary] = field(default_factory=list)


@dataclass
class PlaylistFailure:
    """Per-playlist failure captured during bulk fetch/download."""
    playlist: SpotifyPlaylistSummary
    error_message: str


@dataclass
class BulkDownloadProgress:
    """Progress notification emitted while processing multiple playlists."""
    playlist: SpotifyPlaylistSummary
    playlist_index: int
    playlist_total: int
    stage: str
    tracks_completed: int = 0
    tracks_total: int = 0
    message: Optional[str] = None


@dataclass
class BulkDownloadReport:
    """Combined results for a multi-playlist run."""
    results: list[DownloadResult] = field(default_factory=list)
    failures: list[PlaylistFailure] = field(default_factory=list)
