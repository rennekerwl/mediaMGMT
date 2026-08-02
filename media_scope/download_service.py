"""Restart-safe Step 6 rTorrent download state machine."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from media_scope.download_directories import DownloadDirectoryManager
from media_scope.download_models import (
    DownloadCapabilities,
    DownloadRecord,
    DownloadSnapshot,
    DownloadState,
    HealthDownloadInput,
)
from media_scope.exceptions import (
    DownloadPostProcessingError,
    DownloadStorageError,
    RtorrentError,
)
from media_scope.models import JsonObject
from media_scope.rtorrent_client import RtorrentClient

LOGGER = logging.getLogger("media_scope.download")
Monotonic = Callable[[], float]
Sleeper = Callable[[float], None]
WallClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    """Runtime behavior for one full-download controller run."""

    poll_interval_seconds: float = 30
    stall_timeout_seconds: float = 1800
    overall_timeout_seconds: float = 0
    minimum_free_space_bytes: int = 0
    post_completion_policy: str = "stop"
    post_processing_grace_seconds: float = 30


@dataclass(frozen=True, slots=True)
class RuntimeFailure(Exception):
    """An expected monitored-download terminal condition."""

    error_code: str
    status: str
    message: str
    exit_code: int
    diagnostics: JsonObject


class TorrentDownloadService:
    """Validate, start/resume, monitor, and finalize one existing rTorrent item."""

    def __init__(
        self,
        client: RtorrentClient,
        directories: DownloadDirectoryManager,
        policy: DownloadPolicy,
        *,
        job_id: str,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleeper = time.sleep,
        wall_clock: WallClock = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.directories = directories
        self.policy = policy
        self.job_id = job_id
        self.monotonic = monotonic
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.record = DownloadRecord()
        self.latest_snapshot: DownloadSnapshot | None = None

    def run(
        self,
        health: HealthDownloadInput,
        *,
        resume_stalled: bool = False,
        dry_run: bool = False,
    ) -> tuple[JsonObject, int]:
        """Run Step 6 and return a public report with its documented exit code."""
        started = self.monotonic()
        initial: DownloadSnapshot | None = None
        capabilities: DownloadCapabilities | None = None
        storage: JsonObject = {}
        candidate = health.candidate
        self.record.transition(DownloadState.PENDING, self._timestamp())
        self.record.transition(DownloadState.VALIDATING_INPUT, self._timestamp())
        try:
            capabilities = self.client.discover_download_capabilities()
            self.record.transition(DownloadState.LOCATING_TORRENT, self._timestamp())
            if not self.client.torrent_exists(candidate.infohash):
                raise RuntimeFailure(
                    "SELECTED_TORRENT_NOT_FOUND",
                    "SELECTED_TORRENT_NOT_FOUND",
                    "The Step 5 torrent no longer exists in rTorrent; rerun Step 5.",
                    3,
                    {},
                )
            initial = self.client.download_snapshot(candidate.infohash)
            self.latest_snapshot = initial
            self._validate_identity(initial, candidate.infohash)
            previous_job = self.client.get_custom(candidate.infohash, "media_download_job_id")
            previous_state = self.client.get_custom(candidate.infohash, "media_download_state")
            if previous_job and previous_job != self.job_id:
                raise RuntimeFailure(
                    "TORRENT_STATE_INVALID",
                    "TORRENT_STATE_INVALID",
                    "The torrent is owned by a different Step 6 media job.",
                    3,
                    {"existing_job_id": previous_job},
                )
            if previous_state == DownloadState.STALLED.value and not resume_stalled:
                raise RuntimeFailure(
                    "DOWNLOAD_STALLED",
                    "DOWNLOAD_STALLED",
                    "This job was previously marked stalled; use --resume-stalled to retry it.",
                    6,
                    {"stall_reason": "PRIOR_RUN_STALLED"},
                )

            self.record.transition(DownloadState.PREPARING_DIRECTORY, self._timestamp())
            current_directory = _safe_resolve(initial.directory)
            owned = (
                previous_job == self.job_id or current_directory == self.directories.job_directory
            )
            target = self.directories.prepare(existing_owner=owned, dry_run=dry_run)
            initial = self._prepare_torrent(initial, target, dry_run=dry_run)
            self.latest_snapshot = initial

            self.record.transition(DownloadState.CHECKING_DISK_SPACE, self._timestamp())
            storage = self._check_space(initial, at_start=True)
            if dry_run:
                return (
                    self._dry_run_report(health, initial, capabilities, storage, target),
                    0,
                )

            tmdb_id = health.scope.get("tmdb_id", "unknown")
            self.client.tag_download(
                candidate.infohash,
                job_id=self.job_id,
                state=DownloadState.PENDING.value,
                source=health.probe_job_id or "step5",
                tmdb_id=str(tmdb_id),
            )
            if self._is_complete(initial):
                completed = initial
            else:
                if initial.hashing:
                    self.record.transition(DownloadState.HASH_CHECKING, self._timestamp())
                    self.client.set_download_state(
                        candidate.infohash, DownloadState.HASH_CHECKING.value
                    )
                elif not initial.is_active:
                    self.record.transition(DownloadState.STARTING, self._timestamp())
                    self.client.set_download_state(candidate.infohash, DownloadState.STARTING.value)
                    self.client.start(candidate.infohash)
                if not initial.hashing:
                    self.record.transition(DownloadState.DOWNLOADING, self._timestamp())
                    self.client.set_download_state(
                        candidate.infohash, DownloadState.DOWNLOADING.value
                    )
                completed = self._monitor(candidate.infohash, initial, started)
            return self._complete(
                health,
                completed,
                capabilities,
                storage,
                started,
                initial.base_path,
            )
        except RuntimeFailure as exc:
            return (
                self._failure_report(
                    health,
                    exc,
                    initial,
                    capabilities,
                    storage,
                    started,
                ),
                exc.exit_code,
            )
        except (DownloadStorageError, DownloadPostProcessingError) as exc:
            if isinstance(exc, DownloadPostProcessingError) and not dry_run:
                self._stop_and_mark(candidate.infohash, DownloadState.FAILED)
            failure = RuntimeFailure(
                exc.error_code,
                exc.error_code,
                str(exc),
                exc.exit_code,
                {},
            )
            return (
                self._failure_report(
                    health,
                    failure,
                    initial,
                    capabilities,
                    storage,
                    started,
                ),
                exc.exit_code,
            )

    def _prepare_torrent(
        self,
        snapshot: DownloadSnapshot,
        target: Path,
        *,
        dry_run: bool,
    ) -> DownloadSnapshot:
        current = _safe_resolve(snapshot.directory)
        if current == target:
            return snapshot
        base_path = _safe_resolve(snapshot.base_path)
        on_disk_bytes = 0
        if base_path is not None and base_path.exists():
            on_disk_bytes = self.directories.on_disk_size(base_path)
        if (
            current is not None
            and self.directories.probe_root is not None
            and _path_is_under(current, self.directories.probe_root)
            and current.exists()
        ):
            on_disk_bytes = max(on_disk_bytes, self.directories.on_disk_size(current))
        if snapshot.completed_bytes > 0 or on_disk_bytes > 0:
            raise RuntimeFailure(
                "PROBE_DATA_RELOCATION_REQUIRED",
                "PROBE_DATA_RELOCATION_REQUIRED",
                "Verified payload bytes exist outside the permanent directory and were preserved.",
                5,
                {
                    "completed_bytes": snapshot.completed_bytes,
                    "detected_on_disk_bytes": on_disk_bytes,
                    "current_directory": snapshot.directory,
                    "target_directory": str(target),
                },
            )
        if snapshot.is_active and dry_run:
            raise RuntimeFailure(
                "TORRENT_STATE_INVALID",
                "TORRENT_STATE_INVALID",
                "The active torrent cannot be safely redirected during a dry run.",
                3,
                {},
            )
        if dry_run:
            return snapshot
        if snapshot.is_active or snapshot.state != 0:
            self.client.stop(snapshot.infohash)
            snapshot = self.client.download_snapshot(snapshot.infohash)
            self.latest_snapshot = snapshot
            if snapshot.is_active:
                raise RuntimeFailure(
                    "TORRENT_STATE_INVALID",
                    "TORRENT_STATE_INVALID",
                    "rTorrent did not stop the torrent before directory preparation.",
                    3,
                    {},
                )
        self.client.set_download_directory(snapshot.infohash, target)
        updated = self.client.download_snapshot(snapshot.infohash)
        self.latest_snapshot = updated
        if _safe_resolve(updated.directory) != target:
            raise RuntimeFailure(
                "TORRENT_STATE_INVALID",
                "TORRENT_STATE_INVALID",
                "rTorrent did not register the permanent download directory.",
                3,
                {"reported_directory": updated.directory, "expected_directory": str(target)},
            )
        return updated

    def _monitor(
        self,
        infohash: str,
        initial: DownloadSnapshot,
        started: float,
    ) -> DownloadSnapshot:
        previous = initial
        last_progress = self.monotonic()
        self.record.last_progress_at = self._timestamp()
        while True:
            if not self.client.torrent_exists(infohash):
                raise RuntimeFailure(
                    "TORRENT_DISAPPEARED",
                    "TORRENT_DISAPPEARED",
                    "The selected torrent disappeared from rTorrent during download.",
                    3,
                    {},
                )
            current = self.client.download_snapshot(infohash)
            self.latest_snapshot = current
            self._validate_identity(current, infohash)
            self.record.poll_count += 1
            self.record.maximum_connected_peers = max(
                self.record.maximum_connected_peers, current.connected_peers
            )
            self.record.maximum_complete_peers = max(
                self.record.maximum_complete_peers, current.complete_peers
            )
            self.record.rate_samples.append(current.download_rate)
            error = _reported_error(current.message)
            if error:
                self._stop_and_mark(infohash, DownloadState.FAILED)
                code = (
                    "HASH_CHECK_FAILED" if "hash" in error.casefold() else "RTORRENT_REPORTED_ERROR"
                )
                raise RuntimeFailure(
                    code,
                    code,
                    "rTorrent reported a terminal download error.",
                    3,
                    {"rtorrent_message": current.message},
                )
            if self._useful_progress(previous, current):
                last_progress = self.monotonic()
                self.record.last_progress_at = self._timestamp()
            if current.hashing:
                self.record.transition(DownloadState.HASH_CHECKING, self._timestamp())
                self.client.set_download_state(infohash, DownloadState.HASH_CHECKING.value)
            elif not self._is_complete(current):
                self.record.transition(DownloadState.DOWNLOADING, self._timestamp())
            if self._is_complete(current):
                return current

            free = self.directories.free_bytes()
            required = current.left_bytes + self.policy.minimum_free_space_bytes
            if free < required:
                self._stop_and_mark(infohash, DownloadState.PAUSED_LOW_DISK_SPACE)
                raise RuntimeFailure(
                    "LOW_DISK_SPACE_DURING_DOWNLOAD",
                    "PAUSED_LOW_DISK_SPACE",
                    "Free space fell below remaining payload bytes plus the configured reserve.",
                    5,
                    self._space_details(current, free),
                )
            now = self.monotonic()
            if (
                self.policy.overall_timeout_seconds > 0
                and now - started >= self.policy.overall_timeout_seconds
            ):
                self._stop_and_mark(infohash, DownloadState.FAILED)
                raise RuntimeFailure(
                    "DOWNLOAD_TIMEOUT",
                    "DOWNLOAD_TIMEOUT",
                    "The configured overall download timeout elapsed.",
                    7,
                    {"elapsed_seconds": round(now - started, 3)},
                )
            if now - last_progress >= self.policy.stall_timeout_seconds:
                reason = _stall_reason(current)
                self._stop_and_mark(infohash, DownloadState.STALLED)
                raise RuntimeFailure(
                    "DOWNLOAD_STALLED",
                    "DOWNLOAD_STALLED",
                    "No useful payload or hash-check progress occurred before the stall timeout.",
                    6,
                    {
                        "stall_reason": reason,
                        "connected_peers": current.connected_peers,
                        "complete_peers": current.complete_peers,
                        "rtorrent_message": current.message,
                    },
                )
            previous = current
            self.sleep(self.policy.poll_interval_seconds)

    def _complete(
        self,
        health: HealthDownloadInput,
        completed: DownloadSnapshot,
        capabilities: DownloadCapabilities,
        storage: JsonObject,
        started: float,
        base_before_download: str,
    ) -> tuple[JsonObject, int]:
        infohash = health.candidate.infohash
        self.record.transition(DownloadState.COMPLETED, self._timestamp())
        self.client.set_download_state(infohash, DownloadState.COMPLETED.value)
        base_at_completion = completed.base_path
        self.record.transition(DownloadState.POST_PROCESSING_GRACE, self._timestamp())
        self.client.set_download_state(infohash, DownloadState.POST_PROCESSING_GRACE.value)
        grace_started = self.monotonic()
        final = completed
        while self.monotonic() - grace_started < self.policy.post_processing_grace_seconds:
            remaining = self.policy.post_processing_grace_seconds - (
                self.monotonic() - grace_started
            )
            self.sleep(min(self.policy.poll_interval_seconds, remaining))
            if not self.client.torrent_exists(infohash):
                raise RuntimeFailure(
                    "TORRENT_DISAPPEARED",
                    "POST_PROCESSING_FAILED",
                    "The completed torrent disappeared during post-processing grace.",
                    8,
                    {},
                )
            final = self.client.download_snapshot(infohash)
            self.latest_snapshot = final
            self._validate_identity(final, infohash)
        if not self._is_complete(final):
            raise RuntimeFailure(
                "POST_PROCESSING_FAILED",
                "POST_PROCESSING_FAILED",
                "The torrent no longer reports a complete, verified payload after grace.",
                8,
                {},
            )
        final_path = self.directories.validate_final_path(final.base_path)
        top_level = self.directories.top_level_paths(final_path, self.directories.job_directory)
        payload_size = self.directories.on_disk_size(final_path)
        if self.policy.post_completion_policy == "stop":
            self.client.stop(infohash)
            final_state = "stopped"
        else:
            if not final.is_active:
                self.client.start(infohash)
            final_state = "seeding"
        self.client.set_download_state(infohash, DownloadState.READY_FOR_TRANSFER.value)
        self.record.transition(DownloadState.READY_FOR_TRANSFER, self._timestamp())
        elapsed = self.monotonic() - started
        return (
            {
                "schema_version": 1,
                "result": "download_completed",
                "job_id": self.job_id,
                "scope": health.scope,
                "candidate": self._candidate_payload(health),
                "policy": self._policy_payload(),
                "storage": storage,
                "download": self._download_payload(final, elapsed),
                "paths": {
                    "download_root": str(self.directories.root),
                    "base_path_before_download": base_before_download,
                    "rtorrent_base_path": base_at_completion,
                    "final_base_path": str(final_path),
                    "top_level_paths": top_level,
                    "payload_size_bytes": payload_size,
                    "path_changed_after_completion": (
                        _safe_resolve(base_at_completion) != final_path
                    ),
                },
                "rtorrent": {
                    "rpc_endpoint": self.client.sanitized_endpoint,
                    "client_version": capabilities.client_version,
                    "final_state": final_state,
                    "torrent_retained": True,
                },
                "state_transitions": self.record.transitions,
                "status": DownloadState.READY_FOR_TRANSFER.value,
                "ready_for_transfer": True,
                "warnings": self.record.warnings,
            },
            0,
        )

    def _check_space(self, snapshot: DownloadSnapshot, *, at_start: bool) -> JsonObject:
        free = self.directories.free_bytes()
        remaining = max(snapshot.size_bytes - snapshot.completed_bytes, snapshot.left_bytes, 0)
        details = self._space_details(snapshot, free)
        details.update(
            {
                "download_directory": str(self.directories.job_directory),
                "filesystem_free_bytes_at_start": free if at_start else None,
            }
        )
        if free < remaining + self.policy.minimum_free_space_bytes:
            raise RuntimeFailure(
                "INSUFFICIENT_DISK_SPACE",
                "INSUFFICIENT_DISK_SPACE",
                "Free space is less than remaining payload bytes plus the configured reserve.",
                5,
                details,
            )
        return details

    def _space_details(self, snapshot: DownloadSnapshot, free: int) -> JsonObject:
        remaining = max(snapshot.size_bytes - snapshot.completed_bytes, snapshot.left_bytes, 0)
        return {
            "torrent_size_bytes": snapshot.size_bytes,
            "completed_bytes": snapshot.completed_bytes,
            "remaining_bytes": remaining,
            "filesystem_free_bytes": free,
            "required_reserve_bytes": self.policy.minimum_free_space_bytes,
        }

    def _validate_identity(self, snapshot: DownloadSnapshot, expected: str) -> None:
        if snapshot.infohash.casefold() != expected.casefold():
            raise RuntimeFailure(
                "INFOHASH_MISMATCH",
                "INFOHASH_MISMATCH",
                "rTorrent returned a different hash for the selected item.",
                3,
                {},
            )
        if not snapshot.metadata_retrieved or snapshot.size_bytes <= 0:
            raise RuntimeFailure(
                "TORRENT_STILL_META",
                "TORRENT_STILL_META",
                "The selected rTorrent item is still metadata-only.",
                3,
                {},
            )

    @staticmethod
    def _is_complete(snapshot: DownloadSnapshot) -> bool:
        return (
            snapshot.complete
            and snapshot.size_bytes > 0
            and snapshot.completed_bytes >= snapshot.size_bytes
            and snapshot.left_bytes == 0
            and snapshot.download_rate == 0
            and not snapshot.hashing
            and _reported_error(snapshot.message) is None
        )

    @staticmethod
    def _useful_progress(previous: DownloadSnapshot, current: DownloadSnapshot) -> bool:
        return (
            current.completed_bytes > previous.completed_bytes
            or current.left_bytes < previous.left_bytes
            or current.progress_percent > previous.progress_percent
            or (current.hashing and not previous.hashing)
            or (current.complete and not previous.complete)
        )

    def _stop_and_mark(self, infohash: str, state: DownloadState) -> None:
        try:
            self.client.stop(infohash)
            self.client.set_download_state(infohash, state.value)
            self.record.transition(state, self._timestamp())
        except RtorrentError:
            LOGGER.exception("Could not stop and mark torrent %s.", _short_hash(infohash))

    def _download_payload(self, snapshot: DownloadSnapshot, elapsed: float) -> JsonObject:
        rates = self.record.rate_samples
        average = round(sum(rates) / len(rates)) if rates else 0
        estimated = (
            round(snapshot.left_bytes / snapshot.download_rate)
            if snapshot.download_rate > 0
            else None
        )
        return {
            "status": "DOWNLOAD_COMPLETED",
            "elapsed_seconds": round(elapsed, 3),
            "progress_percent": snapshot.progress_percent,
            "completed_bytes": snapshot.completed_bytes,
            "remaining_bytes": snapshot.left_bytes,
            "download_rate_bytes_per_second": snapshot.download_rate,
            "uploaded_bytes": snapshot.uploaded_bytes,
            "connected_peers": snapshot.connected_peers,
            "complete_peers": snapshot.complete_peers,
            "maximum_connected_peers": self.record.maximum_connected_peers,
            "maximum_complete_peers": self.record.maximum_complete_peers,
            "average_download_rate_bytes_per_second": average,
            "estimated_remaining_seconds": estimated,
            "poll_count": self.record.poll_count,
            "last_progress_at": self.record.last_progress_at,
        }

    def _dry_run_report(
        self,
        health: HealthDownloadInput,
        snapshot: DownloadSnapshot,
        capabilities: DownloadCapabilities,
        storage: JsonObject,
        target: Path,
    ) -> JsonObject:
        return {
            "schema_version": 1,
            "result": "dry_run",
            "job_id": self.job_id,
            "scope": health.scope,
            "candidate": self._candidate_payload(health),
            "policy": self._policy_payload(),
            "storage": storage,
            "rtorrent": {
                "rpc_endpoint": self.client.sanitized_endpoint,
                "client_version": capabilities.client_version,
                "torrent_found": True,
                "metadata_available": snapshot.metadata_retrieved,
                "current_directory": snapshot.directory,
                "planned_directory": str(target),
            },
            "would_start_or_resume": not self._is_complete(snapshot),
            "mutations_performed": False,
            "ready_for_transfer": False,
            "state_transitions": self.record.transitions,
            "warnings": ["Dry-run mode did not change or start the torrent."],
        }

    def _failure_report(
        self,
        health: HealthDownloadInput,
        failure: RuntimeFailure,
        snapshot: DownloadSnapshot | None,
        capabilities: DownloadCapabilities | None,
        storage: JsonObject,
        started: float,
    ) -> JsonObject:
        if not self.record.transitions or self.record.transitions[-1].get("state") not in {
            DownloadState.STALLED.value,
            DownloadState.PAUSED_LOW_DISK_SPACE.value,
        }:
            self.record.transition(DownloadState.FAILED, self._timestamp())
        download: JsonObject = {
            "status": failure.status,
            "elapsed_seconds": round(self.monotonic() - started, 3),
        }
        snapshot = self.latest_snapshot or snapshot
        if snapshot is not None:
            download.update(
                {
                    "progress_percent": snapshot.progress_percent,
                    "completed_bytes": snapshot.completed_bytes,
                    "remaining_bytes": snapshot.left_bytes,
                    "download_rate_bytes_per_second": snapshot.download_rate,
                    "uploaded_bytes": snapshot.uploaded_bytes,
                    "connected_peers": snapshot.connected_peers,
                    "complete_peers": snapshot.complete_peers,
                    "estimated_remaining_seconds": (
                        round(snapshot.left_bytes / snapshot.download_rate)
                        if snapshot.download_rate > 0
                        else None
                    ),
                    "last_progress_at": self.record.last_progress_at,
                }
            )
        return {
            "schema_version": 1,
            "result": "download_failed",
            "job_id": self.job_id,
            "scope": health.scope,
            "candidate": self._candidate_payload(health),
            "error_code": failure.error_code,
            "message": failure.message,
            "status": failure.status,
            "ready_for_transfer": False,
            "policy": self._policy_payload(),
            "storage": storage,
            "download": download,
            "diagnostics": failure.diagnostics,
            "rtorrent": {
                "rpc_endpoint": self.client.sanitized_endpoint,
                "client_version": capabilities.client_version if capabilities else None,
                "torrent_retained": failure.error_code
                not in {"SELECTED_TORRENT_NOT_FOUND", "TORRENT_DISAPPEARED"},
            },
            "state_transitions": self.record.transitions,
            "warnings": self.record.warnings,
        }

    def _candidate_payload(self, health: HealthDownloadInput) -> JsonObject:
        candidate = health.candidate
        return {
            "original_rank": candidate.original_rank,
            "infohash": candidate.infohash,
            "release_title": candidate.release_title,
            "magnet_uri": candidate.magnet_uri,
        }

    def _policy_payload(self) -> JsonObject:
        return {
            "poll_interval_seconds": self.policy.poll_interval_seconds,
            "stall_timeout_seconds": self.policy.stall_timeout_seconds,
            "overall_timeout_seconds": self.policy.overall_timeout_seconds,
            "post_completion_policy": self.policy.post_completion_policy,
            "post_processing_grace_seconds": self.policy.post_processing_grace_seconds,
        }

    def _timestamp(self) -> str:
        return self.wall_clock().isoformat().replace("+00:00", "Z")


def make_download_job_id(health: HealthDownloadInput) -> str:
    """Return a deterministic identity so reruns find the same Step 6 job."""
    tmdb = re.sub(r"[^0-9A-Za-z_-]", "-", str(health.scope.get("tmdb_id", "unknown")))
    return f"download-{tmdb}-{health.candidate.infohash[:12].lower()}"


def _reported_error(message: str | None) -> str | None:
    if not message:
        return None
    lowered = message.casefold()
    terminal = (
        "permission denied",
        "no space left",
        "disk full",
        "input/output error",
        "hash check failed",
        "could not open file",
        "failed to save",
    )
    return message if any(value in lowered for value in terminal) else None


def _stall_reason(snapshot: DownloadSnapshot) -> str:
    message = (snapshot.message or "").casefold()
    if _reported_error(snapshot.message):
        return "RTORRENT_REPORTED_ERROR"
    if any(value in message for value in ("tracker", "network", "resolve", "timed out")):
        return "TRACKER_OR_NETWORK_ERROR"
    if snapshot.connected_peers <= 0:
        return "NO_CONNECTED_PEERS"
    return "CONNECTED_BUT_NO_PROGRESS"


def _safe_resolve(value: str) -> Path | None:
    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else None


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _short_hash(value: str) -> str:
    return f"{value[:8]}...{value[-4:]}"
