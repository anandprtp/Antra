"""
Structured events emitted by the download engine.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from antra.core.models import TrackMetadata


class EngineEventType(Enum):
    PLAYLIST_STARTED = "playlist_started"
    PLAYLIST_COMPLETED = "playlist_completed"
    PLAYLIST_CANCELLED = "playlist_cancelled"
    TRACK_STARTED = "track_started"
    TRACK_SKIPPED = "track_skipped"
    TRACK_RESOLVED = "track_resolved"
    TRACK_DOWNLOAD_ATTEMPT = "track_download_attempt"
    TRACK_COMPLETED = "track_completed"
    TRACK_FAILED = "track_failed"
    # Job-level advisory shown to the user; carries `message` only.
    NOTICE = "notice"
    # issue #75 — files left in a playlist folder that the playlist no longer
    # contains. Advisory: nothing has been deleted when this fires unless
    # playlist_prune_orphans is "auto", which the `pruned` flag states.
    PLAYLIST_ORPHANS = "playlist_orphans"


@dataclass
class EngineEvent:
    type: EngineEventType
    track: Optional[TrackMetadata] = None
    track_index: Optional[int] = None
    track_total: Optional[int] = None
    message: Optional[str] = None
    source: Optional[str] = None
    quality_label: Optional[str] = None
    attempt: Optional[int] = None
    file_path: Optional[str] = None
    error: Optional[str] = None
    # TRACK_COMPLETED only: "unsupported" | "failed" when the tags were not written.
    tags_problem: Optional[str] = None
    # issue #75 — PLAYLIST_ORPHANS payload.
    paths: Optional[list[str]] = None
    pruned: bool = False
