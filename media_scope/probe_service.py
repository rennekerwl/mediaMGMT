"""Deterministic live rTorrent metadata-probe state machine."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from media_scope.exceptions import (
    RtorrentConfigurationError,
    RtorrentError,
    RtorrentRpcError,
    RtorrentRpcFault,
)
from media_scope.magnet_resolver import MagnetResolutionError, validate_magnet_uri
from media_scope.models import JsonObject
from media_scope.probe_directories import ProbeDirectoryManager
from media_scope.probe_models import (
    AttemptRecord,
    ProbeCandidate,
    ProbeState,
    RtorrentCapabilities,
    SearchProbeInput,
    TorrentStatus,
)
from media_scope.rtorrent_client import RtorrentClient

LOGGER = logging.getLogger("media_scope.probe")
Monotonic = Callable[[], float]
Sleeper = Callable[[float], None]
WallClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ProbePolicy:
    """Runtime limits for one deterministic probe command."""

    maximum_candidates: int = 10
    metadata_timeout_seconds: float = 300
    poll_interval_seconds: float = 5
    preflight_timeout_seconds: float = 120
    keep_failed_probes: bool = False


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """Internal result from observing one torrent until a deadline."""

    status: str
    elapsed: float
    poll_count: int
    metadata_retrieved: bool
    detection_method: str | None
    max_connected: int
    max_complete: int
    message: str | None


class TorrentProbeService:
    """Probe candidates in rank order and retain only the first healthy torrent."""

    def __init__(
        self,
        client: RtorrentClient,
        directories: ProbeDirectoryManager,
        policy: ProbePolicy,
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
        self.cleanup_failed = False

    def run(
        self,
        search_input: SearchProbeInput,
        *,
        preflight_magnet: str | None,
        skip_preflight: bool,
    ) -> tuple[JsonObject, int]:
        """Run capability discovery, optional preflight, and ranked candidate probes."""
        capabilities = self.client.discover_capabilities()
        self.directories.prepare_job()
        warnings: list[str] = []
        preflight = self._run_preflight(
            preflight_magnet,
            skip=skip_preflight,
            warnings=warnings,
        )
        base = self._base_report(search_input, capabilities, preflight, warnings)
        if preflight["status"] == "FAILED":
            base.update(
                {
                    "result": "RTORRENT_NETWORK_UNHEALTHY",
                    "error_code": "RTORRENT_NETWORK_UNHEALTHY",
                    "message": (
                        "The known-good preflight torrent could not retrieve metadata; "
                        "candidate health was not evaluated."
                    ),
                    "attempts": [],
                    "selected_candidate": None,
                    "unattempted_candidates": [
                        self._unattempted(value) for value in search_input.candidates
                    ],
                }
            )
            self.directories.cleanup_empty_job()
            return base, 7 if self.cleanup_failed else 5

        candidates = search_input.candidates[: self.policy.maximum_candidates]
        attempts: list[JsonObject] = []
        selected: JsonObject | None = None
        attempted_ranks: set[int] = set()
        for candidate in candidates:
            LOGGER.info(
                "Probing rank %s, hash %s.", candidate.rank, _short_hash(candidate.infohash)
            )
            record = self._probe_candidate(candidate)
            attempted_ranks.add(candidate.rank)
            attempts.append(record.to_dict())
            if record.status in {"METADATA_RETRIEVED", "METADATA_AVAILABLE_PREEXISTING"}:
                selected = self._selected_candidate(candidate, record)
                LOGGER.info(
                    "Selected rank %s, hash %s.",
                    candidate.rank,
                    _short_hash(candidate.infohash),
                )
                break

        unattempted = [
            self._unattempted(value)
            for value in search_input.candidates
            if value.rank not in attempted_ranks
        ]
        base.update(
            {
                "result": (
                    "candidate_health_validated" if selected else "NO_HEALTHY_TORRENT_FOUND"
                ),
                "attempts": attempts,
                "selected_candidate": selected,
                "unattempted_candidates": unattempted,
            }
        )
        if not selected:
            base["error_code"] = "NO_HEALTHY_TORRENT_FOUND"
            base["message"] = "No attempted candidate retrieved metadata before its timeout."
        self.directories.cleanup_empty_job()
        if self.cleanup_failed:
            base["warnings"].append(
                "At least one failed probe could not be fully cleaned up; "
                "operator attention is required."
            )
            return base, 7
        return base, 0 if selected else 6

    def _probe_candidate(self, candidate: ProbeCandidate) -> AttemptRecord:
        started = self.monotonic()
        record = AttemptRecord(candidate, self._timestamp())
        record.transition(ProbeState.PENDING, self._timestamp())
        target = candidate.infohash.upper()
        record.rtorrent_hash = target
        created = False
        directory: Path | None = None
        try:
            record.preexisting = self.client.torrent_exists(candidate.infohash)
            if not record.preexisting:
                directory = self.directories.prepare_candidate(candidate.infohash)
                record.transition(ProbeState.SUBMITTING, self._timestamp())
                record.submission_method = self.client.submit_magnet(
                    candidate.magnet_uri,
                    candidate.infohash,
                    directory,
                )
                created = True
                if not self._confirm_submission(candidate.infohash):
                    record.status = "TORRENT_NOT_FOUND_AFTER_SUBMISSION"
                    record.transition(ProbeState.SUBMISSION_FAILED, self._timestamp())
                    return self._finish_failure(record, started, created, directory)
                self.client.tag_probe(
                    candidate.infohash,
                    job_id=self.job_id,
                    state="waiting_for_metadata",
                    rank=candidate.rank,
                )
            record.transition(ProbeState.WAITING_FOR_METADATA, self._timestamp())
            outcome = self._wait_for_metadata(
                candidate.infohash, self.policy.metadata_timeout_seconds
            )
            self._apply_outcome(record, outcome)
            if outcome.status in {"METADATA_RETRIEVED", "METADATA_AVAILABLE_PREEXISTING"}:
                record.transition(ProbeState.METADATA_RETRIEVED, self._timestamp())
                if created:
                    self.client.stop(candidate.infohash)
                    self.client.set_probe_state(
                        candidate.infohash,
                        "validated_waiting_for_download",
                    )
                record.transition(ProbeState.SELECTED, self._timestamp())
                record.status = (
                    "METADATA_AVAILABLE_PREEXISTING" if record.preexisting else "METADATA_RETRIEVED"
                )
            else:
                state = (
                    ProbeState.METADATA_TIMEOUT
                    if outcome.status == "METADATA_TIMEOUT"
                    else ProbeState.RPC_FAILED
                )
                record.transition(state, self._timestamp())
                record.status = outcome.status
                return self._finish_failure(record, started, created, directory)
        except RtorrentError as exc:
            LOGGER.warning(
                "Probe rank %s failed through rTorrent: %s",
                candidate.rank,
                exc.error_code,
            )
            record.warnings.append(str(exc))
            submission_failure = record.status == ProbeState.SUBMITTING
            if not created and not record.preexisting:
                try:
                    created = self.client.torrent_exists(candidate.infohash)
                except RtorrentError:
                    created = False
            record.transition(
                ProbeState.SUBMISSION_FAILED if submission_failure else ProbeState.RPC_FAILED,
                self._timestamp(),
            )
            record.status = "SUBMISSION_FAILED" if submission_failure else "RPC_FAILED"
            return self._finish_failure(record, started, created, directory)
        record.elapsed_seconds = self.monotonic() - started
        record.finished_at = self._timestamp()
        return record

    def _wait_for_metadata(self, infohash: str, timeout: float) -> WaitOutcome:
        started = self.monotonic()
        deadline = started + timeout
        polls = 0
        max_connected = 0
        max_complete = 0
        message: str | None = None
        consecutive_rpc_failures = 0
        detection: str | None = None
        while True:
            if not self.client.torrent_exists(infohash):
                return WaitOutcome(
                    "TORRENT_DISAPPEARED",
                    self.monotonic() - started,
                    polls,
                    False,
                    detection,
                    max_connected,
                    max_complete,
                    message,
                )
            try:
                status = self.client.status(infohash)
                consecutive_rpc_failures = 0
            except (RtorrentRpcError, RtorrentRpcFault):
                consecutive_rpc_failures += 1
                if consecutive_rpc_failures >= 3:
                    return WaitOutcome(
                        "RPC_FAILED",
                        self.monotonic() - started,
                        polls,
                        False,
                        detection,
                        max_connected,
                        max_complete,
                        message,
                    )
                status = None
            if isinstance(status, TorrentStatus):
                polls += 1
                detection = status.detection_method
                max_connected = max(max_connected, status.connected_peers)
                max_complete = max(max_complete, status.complete_peers)
                message = status.message or message
                if status.metadata_retrieved:
                    return WaitOutcome(
                        "METADATA_RETRIEVED",
                        self.monotonic() - started,
                        polls,
                        True,
                        detection,
                        max_connected,
                        max_complete,
                        message,
                    )
            now = self.monotonic()
            if now >= deadline:
                return WaitOutcome(
                    "METADATA_TIMEOUT",
                    now - started,
                    polls,
                    False,
                    detection,
                    max_connected,
                    max_complete,
                    message,
                )
            self.sleep(min(self.policy.poll_interval_seconds, deadline - now))

    def _confirm_submission(self, infohash: str) -> bool:
        if self.client.torrent_exists(infohash):
            return True
        deadline = self.monotonic() + min(10, self.policy.metadata_timeout_seconds)
        while self.monotonic() < deadline:
            self.sleep(min(0.25, deadline - self.monotonic()))
            if self.client.torrent_exists(infohash):
                return True
        return False

    def _finish_failure(
        self,
        record: AttemptRecord,
        started: float,
        created: bool,
        directory: Path | None,
    ) -> AttemptRecord:
        outcome = record.status
        if created:
            self._cleanup_owned(record, directory)
        elif record.preexisting:
            record.cleanup_status = "SKIPPED_PREEXISTING"
        record.status = outcome
        record.elapsed_seconds = self.monotonic() - started
        record.finished_at = self._timestamp()
        return record

    def _cleanup_owned(self, record: AttemptRecord, directory: Path | None) -> None:
        if self.policy.keep_failed_probes:
            record.cleanup_status = "SKIPPED_KEEP_FAILED_PROBES"
            record.warnings.append(
                "Cleanup was disabled by the dangerous --keep-failed-probes option."
            )
            return
        record.transition(ProbeState.CLEANING_UP, self._timestamp())
        errors: list[str] = []
        try:
            if self.client.torrent_exists(record.candidate.infohash):
                try:
                    self.client.stop(record.candidate.infohash)
                except RtorrentError as exc:
                    errors.append(f"stop failed: {exc}")
                self.client.erase(record.candidate.infohash)
            if self.client.torrent_exists(record.candidate.infohash):
                errors.append("torrent still exists after erase")
        except RtorrentError as exc:
            errors.append(f"rTorrent cleanup failed: {exc}")
        try:
            if directory is not None:
                self.directories.cleanup_candidate(directory)
        except (OSError, RtorrentConfigurationError) as exc:
            errors.append(f"probe-directory cleanup failed: {exc}")
        record.cleanup_performed = not errors
        if errors:
            record.cleanup_status = "CLEANUP_FAILED"
            record.warnings.extend(errors)
            self.cleanup_failed = True
        else:
            record.cleanup_status = "CLEANED_UP"
            record.transition(ProbeState.CLEANED_UP, self._timestamp())

    def _run_preflight(
        self,
        magnet: str | None,
        *,
        skip: bool,
        warnings: list[str],
    ) -> JsonObject:
        if skip:
            return {"status": "SKIPPED", "elapsed_seconds": 0}
        if not magnet:
            warnings.append(
                "No preflight magnet is configured; candidate timeouts cannot be cleanly "
                "distinguished from general rTorrent networking failure."
            )
            return {"status": "NOT_CONFIGURED", "elapsed_seconds": 0}
        try:
            validated = validate_magnet_uri(magnet)
        except MagnetResolutionError as exc:
            raise RtorrentConfigurationError("RTORRENT_PREFLIGHT_MAGNET is invalid.") from exc
        started = self.monotonic()
        preexisting = self.client.torrent_exists(validated.infohash)
        directory: Path | None = None
        created = False
        outcome_status = "FAILED"
        detail = "Known-good preflight metadata was not retrieved."
        try:
            if not preexisting:
                directory = self.directories.prepare_candidate(validated.infohash)
                try:
                    self.client.submit_magnet(validated.magnet_uri, validated.infohash, directory)
                    created = True
                except RtorrentError:
                    try:
                        created = self.client.torrent_exists(validated.infohash)
                    except RtorrentError:
                        created = False
                    raise
                if not self._confirm_submission(validated.infohash):
                    return {
                        "status": "FAILED",
                        "elapsed_seconds": round(self.monotonic() - started, 3),
                        "message": "Preflight torrent did not appear after submission.",
                    }
                self.client.tag_probe(
                    validated.infohash,
                    job_id=self.job_id,
                    state="preflight",
                    rank="preflight",
                )
            outcome = self._wait_for_metadata(
                validated.infohash,
                self.policy.preflight_timeout_seconds,
            )
            if outcome.metadata_retrieved:
                outcome_status = "PASSED"
                detail = "Known-good preflight metadata was retrieved."
            else:
                detail = f"Known-good preflight ended with {outcome.status}."
        except RtorrentError as exc:
            detail = f"Known-good preflight failed: {exc}"
        finally:
            if created:
                try:
                    if self.client.torrent_exists(validated.infohash):
                        self.client.stop(validated.infohash)
                        self.client.erase(validated.infohash)
                    if directory is not None:
                        self.directories.cleanup_candidate(directory)
                except (RtorrentError, OSError, RtorrentConfigurationError) as exc:
                    self.cleanup_failed = True
                    warnings.append(f"Preflight cleanup failed: {exc}")
        return {
            "status": outcome_status,
            "elapsed_seconds": round(self.monotonic() - started, 3),
            "preexisting": preexisting,
            "message": detail,
        }

    def _apply_outcome(self, record: AttemptRecord, outcome: WaitOutcome) -> None:
        record.elapsed_seconds = outcome.elapsed
        record.poll_count = outcome.poll_count
        record.metadata_retrieved = outcome.metadata_retrieved
        record.metadata_detection_method = outcome.detection_method
        record.maximum_connected_peers = outcome.max_connected
        record.maximum_complete_peers = outcome.max_complete
        record.last_rtorrent_message = outcome.message
        if record.preexisting and outcome.metadata_retrieved:
            record.status = "METADATA_AVAILABLE_PREEXISTING"
        else:
            record.status = outcome.status

    def _base_report(
        self,
        search_input: SearchProbeInput,
        capabilities: RtorrentCapabilities,
        preflight: JsonObject,
        warnings: list[str],
    ) -> JsonObject:
        return {
            "schema_version": 1,
            "result": "probe_in_progress",
            "job_id": self.job_id,
            "scope": search_input.scope,
            "rtorrent": {
                "client_version": capabilities.client_version,
                "library_version": capabilities.library_version,
                "api_version": capabilities.api_version,
                "rpc_endpoint": self.client.sanitized_endpoint,
                "load_method": capabilities.load_method,
                "metadata_detection_method": capabilities.metadata_detection_method,
            },
            "preflight": preflight,
            "policy": {
                "metadata_timeout_seconds": self.policy.metadata_timeout_seconds,
                "poll_interval_seconds": self.policy.poll_interval_seconds,
                "maximum_candidates": self.policy.maximum_candidates,
                "content_validation_performed": False,
                "stop_after_first_healthy": True,
                "keep_failed_probes": self.policy.keep_failed_probes,
            },
            "warnings": warnings,
        }

    def _selected_candidate(
        self,
        candidate: ProbeCandidate,
        record: AttemptRecord,
    ) -> JsonObject:
        return {
            "original_rank": candidate.rank,
            "original_score": candidate.score,
            "validated_rank": 1,
            "infohash": candidate.infohash,
            "magnet_uri": candidate.magnet_uri,
            "release_title": candidate.release_title,
            "rtorrent_state": "preexisting_unchanged" if record.preexisting else "stopped",
            "rtorrent_hash": candidate.infohash.upper(),
            "status": "READY_FOR_DOWNLOAD",
            "preexisting": record.preexisting,
        }

    @staticmethod
    def _unattempted(candidate: ProbeCandidate) -> JsonObject:
        return {
            "original_rank": candidate.rank,
            "original_score": candidate.score,
            "infohash": candidate.infohash,
            "release_title": candidate.release_title,
            "reason": "NOT_ATTEMPTED",
        }

    def _timestamp(self) -> str:
        return self.wall_clock().isoformat().replace("+00:00", "Z")


def _short_hash(value: str) -> str:
    return f"{value[:8]}…{value[-4:]}"
