"""Models and state records for deterministic rTorrent metadata probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from media_scope.models import JsonObject


class ProbeState(StrEnum):
    """Explicit lifecycle states recorded for each attempted torrent."""

    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    WAITING_FOR_METADATA = "WAITING_FOR_METADATA"
    METADATA_RETRIEVED = "METADATA_RETRIEVED"
    METADATA_TIMEOUT = "METADATA_TIMEOUT"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    RPC_FAILED = "RPC_FAILED"
    CLEANING_UP = "CLEANING_UP"
    CLEANED_UP = "CLEANED_UP"
    SELECTED = "SELECTED"


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    """One validated candidate from the Jackett search handoff."""

    rank: int
    magnet_uri: str
    infohash: str
    raw: JsonObject

    @property
    def release_title(self) -> str | None:
        value = self.raw.get("original_title")
        return value if isinstance(value, str) else None

    @property
    def score(self) -> int | float | None:
        value = self.raw.get("score")
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class SearchProbeInput:
    """Validated search report consumed by the probe service."""

    scope: JsonObject
    candidates: tuple[ProbeCandidate, ...]


@dataclass(frozen=True, slots=True)
class RtorrentCapabilities:
    """Connected rTorrent versions and selected compatibility operations."""

    client_version: str | None
    library_version: str | None
    api_version: str | None
    methods: frozenset[str]
    load_method: str | None
    load_starts: bool
    metadata_detection_method: str | None


@dataclass(frozen=True, slots=True)
class TorrentStatus:
    """Small, content-agnostic rTorrent status snapshot."""

    metadata_retrieved: bool
    detection_method: str
    connected_peers: int = 0
    complete_peers: int = 0
    message: str | None = None


@dataclass(slots=True)
class AttemptRecord:
    """Mutable in-memory record serialized after an attempt finishes."""

    candidate: ProbeCandidate
    started_at: str
    transitions: list[JsonObject] = field(default_factory=list)
    status: str = ProbeState.PENDING
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    poll_count: int = 0
    metadata_retrieved: bool = False
    metadata_detection_method: str | None = None
    maximum_connected_peers: int = 0
    maximum_complete_peers: int = 0
    last_rtorrent_message: str | None = None
    preexisting: bool = False
    cleanup_performed: bool = False
    cleanup_status: str = "NOT_REQUIRED"
    rtorrent_hash: str | None = None
    submission_method: str | None = None
    warnings: list[str] = field(default_factory=list)

    def transition(self, state: ProbeState, at: str) -> None:
        """Append one explicit state transition."""
        self.status = state.value
        self.transitions.append({"state": state.value, "at": at})

    def to_dict(self) -> JsonObject:
        """Return the stable public attempt representation."""
        raw = self.candidate.raw
        return {
            "original_rank": self.candidate.rank,
            "original_score": self.candidate.score,
            "validated_rank": 1
            if self.status
            in {
                "METADATA_RETRIEVED",
                "METADATA_AVAILABLE_PREEXISTING",
            }
            else None,
            "release_title": self.candidate.release_title,
            "infohash": self.candidate.infohash,
            "indexer_reported_seeders": raw.get("seeders"),
            "status": self.status,
            "state_transitions": self.transitions,
            "metadata_retrieved": self.metadata_retrieved,
            "metadata_detection_method": self.metadata_detection_method,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "poll_count": self.poll_count,
            "maximum_connected_peers": self.maximum_connected_peers,
            "maximum_complete_peers": self.maximum_complete_peers,
            "last_rtorrent_message": self.last_rtorrent_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "preexisting": self.preexisting,
            "cleanup_performed": self.cleanup_performed,
            "cleanup_status": self.cleanup_status,
            "rtorrent_hash": self.rtorrent_hash,
            "submission_method": self.submission_method,
            "warnings": self.warnings,
        }


def copy_json_object(value: dict[str, Any]) -> JsonObject:
    """Narrow a validated dictionary for public model use."""
    return dict(value)
