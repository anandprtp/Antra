from dataclasses import dataclass
from pathlib import Path

from antra.core.models import TrackMetadata


@dataclass(frozen=True)
class TrackAsset:
    path: Path
    title: str
    artist: str = ""
    album: str = ""
    duration_seconds: float | None = None
    source: str = "library"

    @property
    def display_name(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


@dataclass(frozen=True)
class PlaylistPreview:
    source_url: str
    name: str
    tracks: tuple[TrackMetadata, ...]


@dataclass(frozen=True)
class PlaylistSession:
    token: str
    owner_user_id: int
    chat_id: int
    message_id: int | None
    source_url: str
    name: str
    tracks: tuple[TrackMetadata, ...]
    expires_at: int
