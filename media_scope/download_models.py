"""Models for the restart-safe full-download controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from media_scope.models import JsonObject


class DownloadState(StrEnum):
    """Explicit Step 6 lifecycle states."""

    PENDING = "PENDING"
    VALIDATING_INPUT = "VALIDATING_INPUT"
    LOCATING_TORRENT = "LOCATING_TORRENT"
    PREPARING_DIRECTORY = "PREPARING_DIRECTORY"
    CHECKING_DISK_SPACE = "CHECKING_DISK_SPACE"
    STARTING = "STARTING"
    DOWNLOADING = "DOWNLOADING"
    STALLED = "STALLED"
    PAUSED_LOW_DISK_SPACE = "PAUSED_LOW_DISK_SPACE"
    HASH_CHECKING = "HASH_CHECKING"
    COMPLETED = "COMPLETED"
    POST_PROCESSING_GRACE = "POST_PROCESSING_GRACE"
    READY_FOR_TRANSFER = "READY_FOR_TRANSFER"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DownloadCandidate:
    """Validated selected-candidate handoff from Step 5."""

    original_rank: int | None
    infohash: str
    magnet_uri: str
    release_title: str
    rtorrent_hash: str
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class HealthDownloadInput:
    """Validated Step 5 report consumed by Step 6."""

    scope: JsonObject
    candidate: DownloadCandidate
    probe_job_id: str | None


@dataclass(frozen=True, slots=True)
class DownloadCapabilities:
    """rTorrent operations and version data required by Step 6."""

    client_version: str | None
    library_version: str | None
    api_version: str | None
    methods: frozenset[str]
    metadata_detection_method: str
    hash_method: str


@dataclass(frozen=True, slots=True)
class DownloadSnapshot:
    """One normalized rTorrent status sample."""

    infohash: str
    name: str
    metadata_retrieved: bool
    state: int
    is_active: bool
    is_open: bool
    complete: bool
    completed_bytes: int
    size_bytes: int
    left_bytes: int
    download_rate: int
    upload_rate: int
    uploaded_bytes: int
    connected_peers: int
    complete_peers: int
    message: str | None
    base_path: str
    directory: str
    ratio: int
    hashing: bool

    @property
    def progress_percent(self) -> float:
        """Return bounded byte-derived completion percentage."""
        if self.size_bytes <= 0:
            return 0.0
        return round(min(max(self.completed_bytes / self.size_bytes * 100, 0.0), 100.0), 3)


@dataclass(slots=True)
class DownloadRecord:
    """Mutable metrics and transition history for one run."""

    transitions: list[JsonObject] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    poll_count: int = 0
    maximum_connected_peers: int = 0
    maximum_complete_peers: int = 0
    rate_samples: list[int] = field(default_factory=list)
    last_progress_at: str | None = None

    def transition(self, state: DownloadState, at: str, **details: Any) -> None:
        """Append a transition, avoiding adjacent duplicates."""
        if self.transitions and self.transitions[-1].get("state") == state.value:
            return
        item: JsonObject = {"state": state.value, "at": at}
        item.update(details)
        self.transitions.append(item)
