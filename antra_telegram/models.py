from dataclasses import dataclass
from pathlib import Path


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
