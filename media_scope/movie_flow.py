"""Checkpointed orchestration for the complete movie-acquisition workflow."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from dotenv import load_dotenv

from media_scope.models import JsonObject
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.movie_flow")

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_ATTENTION = 3
EXIT_RETRYABLE = 4
EXIT_INTERNAL = 5

FLOW_SCHEMA_VERSION = 2
LEGACY_FLOW_SCHEMA_VERSION = 1

STATE_RUNNING = "RUNNING"
STATE_RETRYABLE = "RETRYABLE_FAILURE"
STATE_ATTENTION = "ATTENTION_REQUIRED"
STATE_COMPLETED = "COMPLETED"
STATE_NO_ACTION = "NO_ACTION_NEEDED"
STATE_NO_ACQUISITION = "NO_ACQUISITION_AVAILABLE"
STATE_CLEARED = "CLEARED"

TERMINAL_STATES = {STATE_COMPLETED, STATE_NO_ACTION, STATE_NO_ACQUISITION, STATE_CLEARED}
SAFE_INTERRUPTED_STAGES = {"recommendations", "search", "download", "history"}


class FlowError(Exception):
    """Base class for expected orchestrator failures."""


class FlowConfigurationError(FlowError):
    """Raised when the flow storage configuration is unusable."""


class FlowManifestError(FlowError):
    """Raised when persisted flow state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class StageSpec:
    """One subprocess-backed movie stage."""

    key: str
    position: int
    module: str
    artifact_label: str


STAGES = (
    StageSpec("recommendations", 1, "media_scope.recommend_cli", "recommendations"),
    StageSpec("search", 2, "media_scope.movie_search", "movie-search"),
    StageSpec("probe", 3, "media_scope.movie_probe", "movie-probe"),
    StageSpec("download", 4, "media_scope.download_torrent", "download"),
    StageSpec("transfer", 5, "media_scope.movie_transfer", "transfer"),
    StageSpec("history", 6, "media_scope.movie_history", "movie-history"),
)
STAGE_BY_KEY = {stage.key: stage for stage in STAGES}
SHARED_STAGE_KEYS = {"recommendations", "search"}
ITEM_STAGE_KEYS = {"probe", "download", "transfer", "history"}
BATCH_TARGET_COUNT = 3

ProcessRunner = Callable[[list[str], Path | None, Path, Path, Path], int]


def build_parser() -> argparse.ArgumentParser:
    """Build the public movie-flow command parser."""
    parser = argparse.ArgumentParser(
        prog="media-movie-flow",
        description="Run and recover the checkpointed movie-acquisition workflow.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Start or safely continue the current flow.")
    _add_pretty(run)

    status = subparsers.add_parser("status", help="Inspect the latest or a specified run.")
    status.add_argument("--run-id", help="Inspect this run instead of the latest run.")
    _add_pretty(status)

    resume = subparsers.add_parser(
        "resume", help="Retry a remediated download or transfer that requires attention."
    )
    resume.add_argument("--run-id", required=True, help="Attention-required run to resume.")
    _add_pretty(resume)

    clear = subparsers.add_parser(
        "clear", help="Acknowledge an attention state without deleting any media."
    )
    clear.add_argument("--run-id", required=True, help="Attention-required run to clear.")
    clear.add_argument("--reason", required=True, help="Operator reason recorded in the manifest.")
    _add_pretty(clear)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    process_runner: ProcessRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Run the movie-flow command and emit exactly one JSON response."""
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    load_dotenv()

    try:
        root = resolve_flow_root(create=args.command != "status")
        orchestrator = MovieFlowOrchestrator(
            root,
            process_runner=process_runner or run_process,
            now=now or (lambda: datetime.now(UTC)),
        )
        if args.command == "run":
            payload, exit_code = orchestrator.run()
        elif args.command == "status":
            payload, exit_code = orchestrator.status(args.run_id)
        elif args.command == "resume":
            payload, exit_code = orchestrator.resume(args.run_id)
        else:
            payload, exit_code = orchestrator.clear(args.run_id, args.reason)
    except (FlowConfigurationError, FlowManifestError) as exc:
        payload = _error_payload("FLOW_CONFIGURATION_ERROR", str(exc))
        exit_code = EXIT_INPUT
    except OSError as exc:
        LOGGER.exception("Movie-flow persistence failed.")
        payload = _error_payload("FLOW_PERSISTENCE_ERROR", _safe_os_message(exc))
        exit_code = EXIT_INTERNAL
    except Exception:
        LOGGER.exception("Unexpected movie-flow failure.")
        payload = _error_payload(
            "FLOW_INTERNAL_ERROR",
            "An unexpected orchestrator error occurred. Inspect stderr for diagnostics.",
        )
        exit_code = EXIT_INTERNAL

    sys.stdout.write(serialize_json(payload, pretty=args.pretty))
    return exit_code


class MovieFlowOrchestrator:
    """Persist and advance at most one movie flow at a time."""

    def __init__(
        self,
        root: Path,
        *,
        process_runner: ProcessRunner | None = None,
        now: Callable[[], datetime] | None = None,
        working_directory: Path | None = None,
    ) -> None:
        self.root = root
        self.runs = root / "runs"
        self.latest = root / "latest-status.json"
        self.lock_path = root / ".movie-flow.lock"
        self.process_runner = process_runner or run_process
        self.now = now or (lambda: datetime.now(UTC))
        self.working_directory = (working_directory or Path.cwd()).resolve()

    def run(self) -> tuple[JsonObject, int]:
        """Start a new run or continue the latest recoverable run."""
        self._prepare_storage()
        with FlowLock(self.lock_path) as acquired:
            if not acquired:
                return self._already_running(), EXIT_OK
            manifest = self._load_latest(optional=True)
            if manifest is None or str(manifest.get("state")) in TERMINAL_STATES:
                manifest = self._new_manifest()
            elif manifest.get("state") == STATE_ATTENTION:
                return manifest, EXIT_ATTENTION
            return self._continue(manifest)

    def status(self, run_id: str | None) -> tuple[JsonObject, int]:
        """Return persisted status without changing it."""
        if run_id:
            manifest = self._load_run(run_id)
        else:
            manifest = self._load_latest(optional=True)
        if manifest is None:
            return {
                "schema_version": FLOW_SCHEMA_VERSION,
                "result": "flow_not_found",
                "message": "No movie-flow runs have been recorded.",
            }, EXIT_OK
        return manifest, EXIT_OK

    def resume(self, run_id: str) -> tuple[JsonObject, int]:
        """Explicitly retry one remediated attention-required stage."""
        self._prepare_storage()
        with FlowLock(self.lock_path) as acquired:
            if not acquired:
                return self._already_running(), EXIT_OK
            manifest = self._load_current_run(run_id)
            if manifest.get("state") != STATE_ATTENTION:
                raise FlowManifestError("Only an ATTENTION_REQUIRED run can be resumed.")
            if not self._resume_allowed(manifest):
                raise FlowManifestError(self._resume_denied_message(manifest))
            if self._latest_error_code(manifest) == "DOWNLOAD_STALLED":
                manifest["resume_stalled_authorized"] = True
            manifest["state"] = STATE_RUNNING
            manifest["result"] = "flow_running"
            manifest["attention"] = None
            manifest["resume_requested_at"] = self._timestamp()
            self._write_manifest(manifest)
            return self._continue(manifest, recover_interrupted=False)

    def clear(self, run_id: str, reason: str) -> tuple[JsonObject, int]:
        """Acknowledge blocked state without touching external or artifact data."""
        clean_reason = reason.strip()
        if not clean_reason:
            raise FlowManifestError("The clear reason must not be empty.")
        self._prepare_storage()
        with FlowLock(self.lock_path) as acquired:
            if not acquired:
                return self._already_running(), EXIT_OK
            manifest = self._load_current_run(run_id)
            if manifest.get("state") not in {STATE_ATTENTION, STATE_RETRYABLE}:
                raise FlowManifestError(
                    "Only an ATTENTION_REQUIRED or RETRYABLE_FAILURE run can be cleared."
                )
            if self._is_batch_manifest(manifest) and self._cleanup_clear_continues(manifest):
                item = self._current_item(manifest)
                item["status"] = "ACQUIRED"
                item["cleanup_acknowledgement"] = {
                    "acknowledged_at": self._timestamp(),
                    "reason": clean_reason,
                }
                manifest["clear_reason"] = clean_reason
                item.pop("post_history_outcome", None)
                manifest["attention"] = None
                manifest["retryable_failure"] = None
                manifest["state"] = STATE_RUNNING
                manifest["result"] = "flow_running"
                decision = self._advance_to_next_item(manifest)
                if decision is not None:
                    return decision
                return self._continue(manifest, recover_interrupted=False)
            manifest.update(
                {
                    "state": STATE_CLEARED,
                    "result": "flow_cleared",
                    "cleared_at": self._timestamp(),
                    "clear_reason": clean_reason,
                    "message": (
                        "The flow was cleared without deleting local files, remote files, "
                        "torrents, or run history."
                    ),
                    "allowed_actions": ["status"],
                }
            )
            self._write_manifest(manifest)
            return manifest, EXIT_OK

    def _continue(
        self, manifest: JsonObject, *, recover_interrupted: bool = True
    ) -> tuple[JsonObject, int]:
        if self._is_batch_manifest(manifest):
            return self._continue_batch(manifest, recover_interrupted=recover_interrupted)
        return self._continue_legacy(manifest, recover_interrupted=recover_interrupted)

    def _continue_legacy(
        self, manifest: JsonObject, *, recover_interrupted: bool = True
    ) -> tuple[JsonObject, int]:
        if recover_interrupted:
            recovery = self._recover_interrupted(manifest)
            if recovery is not None:
                return recovery

        while True:
            stage_key = self._stage_key(manifest)
            stage = STAGE_BY_KEY[stage_key]
            input_path = self._input_artifact(manifest, stage)
            extra_args = ["--verbose"]
            if stage.key == "download" and manifest.get("resume_stalled_authorized") is True:
                extra_args.append("--resume-stalled")

            payload, exit_code, output_valid = self._execute_stage(
                manifest,
                stage,
                input_path=input_path,
                extra_args=extra_args,
            )
            decision = self._evaluate_stage(
                manifest,
                stage,
                payload,
                exit_code=exit_code,
                output_valid=output_valid,
            )
            if decision is not None:
                return decision

    def _continue_batch(
        self, manifest: JsonObject, *, recover_interrupted: bool = True
    ) -> tuple[JsonObject, int]:
        if recover_interrupted:
            recovery = self._recover_interrupted_batch(manifest)
            if recovery is not None:
                return recovery

        while True:
            stage_key = self._stage_key(manifest)
            stage = STAGE_BY_KEY[stage_key]
            item = None if stage_key in SHARED_STAGE_KEYS else self._current_item(manifest)
            input_path = self._batch_input_artifact(manifest, stage, item)
            extra_args = ["--verbose"]
            if stage.key == "probe" and item is not None:
                extra_args.extend(["--recommendation-rank", str(item["recommendation_rank"])])
            if stage.key == "download" and manifest.get("resume_stalled_authorized") is True:
                extra_args.append("--resume-stalled")

            payload, exit_code, output_valid = self._execute_stage(
                manifest,
                stage,
                input_path=input_path,
                extra_args=extra_args,
                item=item,
            )
            decision = self._evaluate_batch_stage(
                manifest,
                stage,
                payload,
                exit_code=exit_code,
                output_valid=output_valid,
                item=item,
            )
            if decision is not None:
                return decision

    def _execute_stage(
        self,
        manifest: JsonObject,
        stage: StageSpec,
        *,
        input_path: Path | None,
        extra_args: list[str],
        item: JsonObject | None = None,
    ) -> tuple[JsonObject | None, int | None, bool]:
        run_directory = self._run_directory(manifest)
        stage_record = self._stage_record_for(manifest, stage.key, item)
        attempts = stage_record.setdefault("attempts", [])
        if not isinstance(attempts, list):
            raise FlowManifestError(f"Stage {stage.key} has an invalid attempts list.")
        attempt_number = len(attempts) + 1
        item_label = f"item-{int(item['recommendation_rank']):02d}-" if item is not None else ""
        prefix = (
            f"{stage.position:02d}-{item_label}{stage.artifact_label}.attempt-{attempt_number:03d}"
        )
        partial_path = run_directory / f"{prefix}.stdout.partial"
        artifact_path = run_directory / f"{prefix}.json"
        log_path = run_directory / f"{prefix}.stderr.log"
        command = [sys.executable, "-m", stage.module, *extra_args]
        attempt: JsonObject = {
            "attempt": attempt_number,
            "status": "RUNNING",
            "started_at": self._timestamp(),
            "module": stage.module,
            "stdout_partial": str(partial_path),
            "stderr_log": str(log_path),
        }
        attempts.append(attempt)
        stage_record["status"] = "RUNNING"
        if item is not None:
            item["status"] = "PROCESSING"
            item["current_stage"] = stage.key
            manifest["current_item_rank"] = item["recommendation_rank"]
            self._aggregate_stage_record(manifest, stage.key)["status"] = "RUNNING"
        manifest.update(
            {
                "state": STATE_RUNNING,
                "result": "flow_running",
                "current_stage": stage.key,
                "message": (
                    f"Running movie-flow item {item['recommendation_rank']} stage: {stage.key}."
                    if item is not None
                    else f"Running movie-flow stage: {stage.key}."
                ),
                "allowed_actions": ["status"],
            }
        )
        self._write_manifest(manifest)

        try:
            exit_code = self.process_runner(
                command,
                input_path,
                partial_path,
                log_path,
                self.working_directory,
            )
        except OSError as exc:
            attempt.update(
                {
                    "status": "LAUNCH_FAILED",
                    "completed_at": self._timestamp(),
                    "error_code": "CHILD_PROCESS_START_FAILED",
                    "message": _safe_os_message(exc),
                }
            )
            stage_record["status"] = "FAILED"
            self._write_manifest(manifest)
            return None, None, False

        attempt["exit_code"] = exit_code
        attempt["completed_at"] = self._timestamp()
        payload: JsonObject | None = None
        output_valid = False
        try:
            decoded = json.loads(partial_path.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
                output_valid = True
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        if output_valid and payload is not None:
            partial_path.replace(artifact_path)
            attempt.update(
                {
                    "status": "COMPLETED",
                    "artifact": str(artifact_path),
                    "result": payload.get("result"),
                    "error_code": payload.get("error_code"),
                }
            )
            stage_record["latest_artifact"] = str(artifact_path)
        else:
            attempt.update(
                {
                    "status": "INVALID_OUTPUT",
                    "error_code": "INVALID_STAGE_OUTPUT",
                    "message": "The child stage did not emit one valid JSON object.",
                }
            )
        stage_record["status"] = "COMPLETED" if output_valid else "FAILED"
        self._write_manifest(manifest)
        return payload, exit_code, output_valid

    def _evaluate_stage(
        self,
        manifest: JsonObject,
        stage: StageSpec,
        payload: JsonObject | None,
        *,
        exit_code: int | None,
        output_valid: bool,
    ) -> tuple[JsonObject, int] | None:
        result = payload.get("result") if payload else None
        raw_error_code = payload.get("error_code") if payload else None
        raw_message = payload.get("message") if payload else None
        error_code = raw_error_code if isinstance(raw_error_code, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""

        if not output_valid or exit_code is None:
            return self._stage_failure(
                manifest,
                stage,
                error_code="INVALID_STAGE_OUTPUT"
                if exit_code is not None
                else "CHILD_START_FAILED",
                message=message or "The stage did not produce a trustworthy result.",
                attention=(
                    exit_code is not None and stage.key in {"probe", "download", "transfer"}
                ),
            )

        if stage.key == "recommendations":
            if exit_code == 0 and result == "recommendations_not_needed":
                return self._finish(
                    manifest,
                    state=STATE_NO_ACTION,
                    result="flow_no_action_needed",
                    message="The movies directory does not currently need recommendations.",
                )
            if exit_code == 0 and result == "recommendations_created":
                self._advance(manifest, stage)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "RECOMMENDATION_STAGE_FAILED",
                message=message or "The recommendation stage failed.",
                attention=False,
            )

        if stage.key == "search":
            if exit_code == 0 and result == "movie_search_completed":
                self._advance(manifest, stage)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_SEARCH_STAGE_FAILED",
                message=message or "The movie-search stage failed.",
                attention=False,
            )

        if stage.key == "probe":
            if exit_code == 0 and result == "candidate_health_validated":
                selected = payload.get("selected_candidate") if payload else None
                if isinstance(selected, dict) and selected.get("status") == "READY_FOR_DOWNLOAD":
                    self._advance(manifest, stage)
                    return None
                return self._stage_failure(
                    manifest,
                    stage,
                    error_code="INVALID_PROBE_SUCCESS",
                    message="The probe result did not select a READY_FOR_DOWNLOAD candidate.",
                    attention=True,
                )
            if exit_code in {3, 6} and result in {
                "NO_PROBEABLE_CANDIDATES",
                "NO_HEALTHY_TORRENT_FOUND",
            }:
                return self._finish(
                    manifest,
                    state=STATE_NO_ACQUISITION,
                    result="flow_no_acquisition_available",
                    message=message or "No usable movie torrent was available.",
                )
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_PROBE_STAGE_FAILED",
                message=message or "The movie-probe stage failed.",
                attention=exit_code not in {4, 5},
            )

        if stage.key == "download":
            if (
                exit_code == 0
                and result == "download_completed"
                and payload is not None
                and payload.get("status") == "READY_FOR_TRANSFER"
            ):
                self._advance(manifest, stage)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "DOWNLOAD_STAGE_FAILED",
                message=message or "The movie-download stage failed.",
                attention=exit_code != 4,
            )

        if stage.key == "transfer":
            if exit_code == 0 and result == "transfer_completed":
                manifest["post_history_outcome"] = {
                    "type": "completed",
                    "message": "The movie was transferred and seedbox cleanup completed.",
                }
                self._advance(manifest, stage)
                return None
            transfer = payload.get("transfer") if payload else None
            if (
                result == "transfer_completed_cleanup_failed"
                and isinstance(transfer, dict)
                and transfer.get("status") == "COMPLETED"
            ):
                manifest["post_history_outcome"] = {
                    "type": "cleanup_attention",
                    "error_code": error_code or "SEEDBOX_CLEANUP_FAILED",
                    "message": message or "The local copy completed but seedbox cleanup failed.",
                }
                self._advance(manifest, stage, completed_status="PARTIAL_SUCCESS")
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "TRANSFER_STAGE_FAILED",
                message=message or "The movie-transfer stage failed.",
                attention=True,
                resume_allowed=True,
            )

        if stage.key == "history":
            if exit_code == 0 and result in {
                "movie_history_recorded",
                "movie_history_already_recorded",
            }:
                outcome = manifest.get("post_history_outcome")
                if isinstance(outcome, dict) and outcome.get("type") == "cleanup_attention":
                    self._stage_record(manifest, "history")["status"] = "SUCCEEDED"
                    return self._stage_failure(
                        manifest,
                        STAGE_BY_KEY["transfer"],
                        error_code=str(outcome.get("error_code") or "SEEDBOX_CLEANUP_FAILED"),
                        message=str(
                            outcome.get("message")
                            or "The local copy completed but seedbox cleanup failed."
                        ),
                        attention=True,
                        resume_allowed=False,
                        stage_status="PARTIAL_SUCCESS",
                    )
                return self._finish(
                    manifest,
                    state=STATE_COMPLETED,
                    result="flow_completed",
                    message="The movie was transferred and recorded in acquisition history.",
                )
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_HISTORY_STAGE_FAILED",
                message=message or "The movie-history stage failed.",
                attention=False,
            )

        raise FlowManifestError(f"No evaluator exists for stage {stage.key}.")

    def _evaluate_batch_stage(
        self,
        manifest: JsonObject,
        stage: StageSpec,
        payload: JsonObject | None,
        *,
        exit_code: int | None,
        output_valid: bool,
        item: JsonObject | None,
    ) -> tuple[JsonObject, int] | None:
        result = payload.get("result") if payload else None
        raw_error_code = payload.get("error_code") if payload else None
        raw_message = payload.get("message") if payload else None
        error_code = raw_error_code if isinstance(raw_error_code, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""

        if not output_valid or exit_code is None:
            return self._stage_failure(
                manifest,
                stage,
                error_code=(
                    "INVALID_STAGE_OUTPUT" if exit_code is not None else "CHILD_START_FAILED"
                ),
                message=message or "The stage did not produce a trustworthy result.",
                attention=(
                    exit_code is not None and stage.key in {"probe", "download", "transfer"}
                ),
                item=item,
            )

        if stage.key == "recommendations":
            if exit_code == 0 and result == "recommendations_not_needed":
                return self._finish(
                    manifest,
                    state=STATE_NO_ACTION,
                    result="flow_no_action_needed",
                    message="The movies directory does not currently need recommendations.",
                )
            if exit_code == 0 and result == "recommendations_created":
                self._advance_shared_batch_stage(manifest, stage)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "RECOMMENDATION_STAGE_FAILED",
                message=message or "The recommendation stage failed.",
                attention=False,
            )

        if stage.key == "search":
            if exit_code == 0 and result == "movie_search_completed" and payload is not None:
                self._initialize_batch_items(manifest, payload)
                self._aggregate_stage_record(manifest, "search")["status"] = "SUCCEEDED"
                if not self._batch_items(manifest):
                    return self._finish_batch(manifest)
                first = self._batch_items(manifest)[0]
                manifest["current_item_rank"] = first["recommendation_rank"]
                manifest["current_stage"] = "probe"
                manifest["message"] = "Movie search completed; recommendation 1 is next."
                manifest["retryable_failure"] = None
                manifest["attention"] = None
                self._write_manifest(manifest)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_SEARCH_STAGE_FAILED",
                message=message or "The movie-search stage failed.",
                attention=False,
            )

        if item is None:
            raise FlowManifestError(f"Batch stage {stage.key} has no current item.")

        if stage.key == "probe":
            if exit_code == 0 and result == "candidate_health_validated":
                selected = payload.get("selected_candidate") if payload else None
                if isinstance(selected, dict) and selected.get("status") == "READY_FOR_DOWNLOAD":
                    self._advance_item_stage(manifest, item, stage)
                    return None
                return self._stage_failure(
                    manifest,
                    stage,
                    error_code="INVALID_PROBE_SUCCESS",
                    message="The probe result did not select a READY_FOR_DOWNLOAD candidate.",
                    attention=True,
                    item=item,
                )
            if exit_code in {3, 6} and result in {
                "NO_PROBEABLE_CANDIDATES",
                "NO_HEALTHY_TORRENT_FOUND",
            }:
                self._mark_item_skipped(
                    manifest,
                    item,
                    error_code=error_code or str(result),
                    message=message or "No usable torrent was available for this recommendation.",
                )
                return self._advance_to_next_item(manifest)
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_PROBE_STAGE_FAILED",
                message=message or "The movie-probe stage failed.",
                attention=exit_code not in {4, 5},
                item=item,
            )

        if stage.key == "download":
            if (
                exit_code == 0
                and result == "download_completed"
                and payload is not None
                and payload.get("status") == "READY_FOR_TRANSFER"
            ):
                self._advance_item_stage(manifest, item, stage)
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "DOWNLOAD_STAGE_FAILED",
                message=message or "The movie-download stage failed.",
                attention=exit_code != 4,
                item=item,
            )

        if stage.key == "transfer":
            if exit_code == 0 and result == "transfer_completed":
                item["post_history_outcome"] = {
                    "type": "completed",
                    "message": "The movie was transferred and seedbox cleanup completed.",
                }
                self._advance_item_stage(manifest, item, stage)
                return None
            transfer = payload.get("transfer") if payload else None
            if (
                result == "transfer_completed_cleanup_failed"
                and isinstance(transfer, dict)
                and transfer.get("status") == "COMPLETED"
            ):
                item["post_history_outcome"] = {
                    "type": "cleanup_attention",
                    "error_code": error_code or "SEEDBOX_CLEANUP_FAILED",
                    "message": message or "The local copy completed but seedbox cleanup failed.",
                }
                self._advance_item_stage(manifest, item, stage, completed_status="PARTIAL_SUCCESS")
                return None
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "TRANSFER_STAGE_FAILED",
                message=message or "The movie-transfer stage failed.",
                attention=True,
                resume_allowed=True,
                item=item,
            )

        if stage.key == "history":
            if exit_code == 0 and result in {
                "movie_history_recorded",
                "movie_history_already_recorded",
            }:
                self._stage_record_for(manifest, "history", item)["status"] = "SUCCEEDED"
                self._refresh_aggregate_item_stages(manifest)
                outcome = item.get("post_history_outcome")
                if isinstance(outcome, dict) and outcome.get("type") == "cleanup_attention":
                    item["status"] = "ACQUIRED_CLEANUP_REQUIRED"
                    return self._stage_failure(
                        manifest,
                        STAGE_BY_KEY["transfer"],
                        error_code=str(outcome.get("error_code") or "SEEDBOX_CLEANUP_FAILED"),
                        message=str(
                            outcome.get("message")
                            or "The local copy completed but seedbox cleanup failed."
                        ),
                        attention=True,
                        resume_allowed=False,
                        stage_status="PARTIAL_SUCCESS",
                        item=item,
                        clear_continues_batch=True,
                    )
                item["status"] = "ACQUIRED"
                item.pop("post_history_outcome", None)
                return self._advance_to_next_item(manifest)
            return self._stage_failure(
                manifest,
                stage,
                error_code=error_code or "MOVIE_HISTORY_STAGE_FAILED",
                message=message or "The movie-history stage failed.",
                attention=False,
                item=item,
            )

        raise FlowManifestError(f"No batch evaluator exists for stage {stage.key}.")

    def _stage_failure(
        self,
        manifest: JsonObject,
        stage: StageSpec,
        *,
        error_code: str,
        message: str,
        attention: bool,
        resume_allowed: bool | None = None,
        stage_status: str = "FAILED",
        item: JsonObject | None = None,
        clear_continues_batch: bool = False,
    ) -> tuple[JsonObject, int]:
        stage_record = self._stage_record_for(manifest, stage.key, item)
        stage_record["status"] = stage_status
        if item is not None:
            item["status"] = (
                "ACQUIRED_CLEANUP_REQUIRED"
                if clear_continues_batch
                else ("ATTENTION_REQUIRED" if attention else "RETRYABLE_FAILURE")
            )
            item["current_stage"] = stage.key
            aggregate = self._aggregate_stage_record(manifest, stage.key)
            aggregate["status"] = stage_status
        if attention:
            allowed = (
                resume_allowed
                if resume_allowed is not None
                else stage.key in {"download", "transfer"}
            )
            manifest.update(
                {
                    "state": STATE_ATTENTION,
                    "result": "flow_attention_required",
                    "current_stage": stage.key,
                    "message": message,
                    "attention": {
                        "stage": stage.key,
                        "error_code": error_code,
                        "message": message,
                        "resume_allowed": allowed,
                        "guidance": self._attention_guidance(stage.key, error_code, allowed),
                        **(
                            {"recommendation_rank": item["recommendation_rank"]}
                            if item is not None
                            else {}
                        ),
                        **({"clear_continues_batch": True} if clear_continues_batch else {}),
                    },
                    "allowed_actions": (
                        ["status", "resume", "clear"] if allowed else ["status", "clear"]
                    ),
                }
            )
            self._write_manifest(manifest)
            return manifest, EXIT_ATTENTION

        manifest.update(
            {
                "state": STATE_RETRYABLE,
                "result": "flow_retryable_failure",
                "current_stage": stage.key,
                "message": message,
                "retryable_failure": {
                    "stage": stage.key,
                    "error_code": error_code,
                    "message": message,
                    **(
                        {"recommendation_rank": item["recommendation_rank"]}
                        if item is not None
                        else {}
                    ),
                },
                "allowed_actions": ["status", "run"],
            }
        )
        self._write_manifest(manifest)
        return manifest, EXIT_RETRYABLE

    def _recover_interrupted_batch(self, manifest: JsonObject) -> tuple[JsonObject, int] | None:
        if manifest.get("state") != STATE_RUNNING:
            return None
        stage_key = self._stage_key(manifest)
        stage = STAGE_BY_KEY[stage_key]
        item = None if stage_key in SHARED_STAGE_KEYS else self._current_item(manifest)
        stage_record = self._stage_record_for(manifest, stage_key, item)
        attempts = stage_record.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return None
        latest_attempt = attempts[-1]
        if not isinstance(latest_attempt, dict):
            return None
        attempt_status = latest_attempt.get("status")
        if attempt_status == "COMPLETED":
            raw_artifact = latest_attempt.get("artifact")
            exit_code = latest_attempt.get("exit_code")
            if isinstance(raw_artifact, str) and isinstance(exit_code, int):
                payload = self._read_stage_artifact(manifest, Path(raw_artifact))
                return self._evaluate_batch_stage(
                    manifest,
                    stage,
                    payload,
                    exit_code=exit_code,
                    output_valid=True,
                    item=item,
                )
        if attempt_status == "LAUNCH_FAILED":
            return self._stage_failure(
                manifest,
                stage,
                error_code="CHILD_START_FAILED",
                message="The child stage could not be started.",
                attention=False,
                item=item,
            )
        if attempt_status == "INVALID_OUTPUT":
            return self._stage_failure(
                manifest,
                stage,
                error_code="INVALID_STAGE_OUTPUT",
                message="The child stage did not produce a trustworthy result.",
                attention=stage_key in {"probe", "download", "transfer"},
                item=item,
            )
        if attempt_status != "RUNNING":
            return None
        latest_attempt["status"] = "INTERRUPTED"
        latest_attempt["interrupted_at"] = self._timestamp()
        if stage_key in SAFE_INTERRUPTED_STAGES:
            stage_record["status"] = "INTERRUPTED_RETRYABLE"
            if item is not None:
                item["status"] = "PROCESSING"
            manifest["message"] = f"Recovering interrupted {stage_key} stage."
            self._write_manifest(manifest)
            return None
        return self._stage_failure(
            manifest,
            stage,
            error_code="INTERRUPTED_STAGE",
            message=f"The {stage_key} stage was interrupted while external state may have changed.",
            attention=True,
            resume_allowed=stage_key == "transfer",
            item=item,
        )

    def _recover_interrupted(self, manifest: JsonObject) -> tuple[JsonObject, int] | None:
        if manifest.get("state") != STATE_RUNNING:
            return None
        stage_key = self._stage_key(manifest)
        stage = STAGE_BY_KEY[stage_key]
        stage_record = self._stage_record(manifest, stage_key)
        attempts = stage_record.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return None
        latest_attempt = attempts[-1]
        if not isinstance(latest_attempt, dict):
            return None
        attempt_status = latest_attempt.get("status")
        if attempt_status == "COMPLETED":
            raw_artifact = latest_attempt.get("artifact")
            exit_code = latest_attempt.get("exit_code")
            if isinstance(raw_artifact, str) and isinstance(exit_code, int):
                payload = self._read_stage_artifact(manifest, Path(raw_artifact))
                return self._evaluate_stage(
                    manifest,
                    stage,
                    payload,
                    exit_code=exit_code,
                    output_valid=True,
                )
        if attempt_status == "LAUNCH_FAILED":
            return self._stage_failure(
                manifest,
                stage,
                error_code="CHILD_START_FAILED",
                message="The child stage could not be started.",
                attention=False,
            )
        if attempt_status == "INVALID_OUTPUT":
            return self._stage_failure(
                manifest,
                stage,
                error_code="INVALID_STAGE_OUTPUT",
                message="The child stage did not produce a trustworthy result.",
                attention=stage_key in {"probe", "download", "transfer"},
            )
        if attempt_status != "RUNNING":
            return None
        latest_attempt["status"] = "INTERRUPTED"
        latest_attempt["interrupted_at"] = self._timestamp()
        if stage_key in SAFE_INTERRUPTED_STAGES:
            stage_record["status"] = "INTERRUPTED_RETRYABLE"
            manifest["message"] = f"Recovering interrupted {stage_key} stage."
            self._write_manifest(manifest)
            return None
        return self._stage_failure(
            manifest,
            stage,
            error_code="INTERRUPTED_STAGE",
            message=(
                f"The {stage_key} stage was interrupted while external state may have changed."
            ),
            attention=True,
            resume_allowed=stage_key == "transfer",
        )

    def _initialize_batch_items(self, manifest: JsonObject, search_payload: JsonObject) -> None:
        movies = search_payload.get("movies")
        if not isinstance(movies, list):
            raise FlowManifestError("Movie-search output has no valid movies array.")
        items: list[JsonObject] = []
        for rank, movie in enumerate(movies, start=1):
            if not isinstance(movie, dict):
                raise FlowManifestError(f"Movie-search recommendation {rank} is invalid.")
            recommendation = movie.get("recommendation")
            if not isinstance(recommendation, dict):
                raise FlowManifestError(
                    f"Movie-search recommendation {rank} has no valid identity."
                )
            tmdb_id = recommendation.get("tmdb_id")
            title = recommendation.get("title")
            year = recommendation.get("year")
            if (
                not isinstance(tmdb_id, int)
                or isinstance(tmdb_id, bool)
                or tmdb_id <= 0
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(year, int)
                or isinstance(year, bool)
            ):
                raise FlowManifestError(f"Movie-search recommendation {rank} is incomplete.")
            items.append(
                {
                    "recommendation_rank": rank,
                    "recommendation": {
                        "tmdb_id": tmdb_id,
                        "title": title.strip(),
                        "year": year,
                    },
                    "status": "PENDING",
                    "current_stage": "probe",
                    "stages": {
                        key: {
                            "position": STAGE_BY_KEY[key].position,
                            "status": "PENDING",
                            "attempts": [],
                        }
                        for key in ("probe", "download", "transfer", "history")
                    },
                }
            )
        manifest["items"] = items
        self._refresh_batch_summary(manifest)

    def _advance_shared_batch_stage(self, manifest: JsonObject, completed: StageSpec) -> None:
        self._aggregate_stage_record(manifest, completed.key)["status"] = "SUCCEEDED"
        next_stage = STAGES[completed.position]
        manifest.update(
            {
                "state": STATE_RUNNING,
                "result": "flow_running",
                "current_stage": next_stage.key,
                "message": f"Stage {completed.key} completed; {next_stage.key} is next.",
                "retryable_failure": None,
                "attention": None,
                "allowed_actions": ["status"],
            }
        )
        self._write_manifest(manifest)

    def _advance_item_stage(
        self,
        manifest: JsonObject,
        item: JsonObject,
        completed: StageSpec,
        *,
        completed_status: str = "SUCCEEDED",
    ) -> None:
        self._stage_record_for(manifest, completed.key, item)["status"] = completed_status
        if completed.key == "download":
            manifest.pop("resume_stalled_authorized", None)
        item_keys = ("probe", "download", "transfer", "history")
        next_key = item_keys[item_keys.index(completed.key) + 1]
        item["current_stage"] = next_key
        item["status"] = "PROCESSING"
        manifest.update(
            {
                "state": STATE_RUNNING,
                "result": "flow_running",
                "current_stage": next_key,
                "current_item_rank": item["recommendation_rank"],
                "message": (
                    f"Recommendation {item['recommendation_rank']} stage {completed.key} "
                    f"completed; {next_key} is next."
                ),
                "retryable_failure": None,
                "attention": None,
                "allowed_actions": ["status"],
            }
        )
        self._write_manifest(manifest)

    def _mark_item_skipped(
        self,
        manifest: JsonObject,
        item: JsonObject,
        *,
        error_code: str,
        message: str,
    ) -> None:
        item["status"] = "SKIPPED"
        item["skip"] = {"error_code": error_code, "message": message}
        item["current_stage"] = None
        self._stage_record_for(manifest, "probe", item)["status"] = "NO_RESULT"
        for key in ("download", "transfer", "history"):
            self._stage_record_for(manifest, key, item)["status"] = "SKIPPED"

    def _advance_to_next_item(self, manifest: JsonObject) -> tuple[JsonObject, int] | None:
        items = self._batch_items(manifest)
        current_rank = manifest.get("current_item_rank")
        current_index = next(
            (
                index
                for index, value in enumerate(items)
                if value.get("recommendation_rank") == current_rank
            ),
            -1,
        )
        if current_index >= 0 and current_index + 1 < len(items):
            next_item = items[current_index + 1]
            manifest.update(
                {
                    "state": STATE_RUNNING,
                    "result": "flow_running",
                    "current_item_rank": next_item["recommendation_rank"],
                    "current_stage": "probe",
                    "message": (
                        f"Recommendation {current_rank} finished; recommendation "
                        f"{next_item['recommendation_rank']} is next."
                    ),
                    "retryable_failure": None,
                    "attention": None,
                    "allowed_actions": ["status"],
                }
            )
            self._refresh_batch_summary(manifest)
            self._write_manifest(manifest)
            return None
        manifest["current_item_rank"] = None
        self._refresh_batch_summary(manifest)
        return self._finish_batch(manifest)

    def _finish_batch(self, manifest: JsonObject) -> tuple[JsonObject, int]:
        summary = self._refresh_batch_summary(manifest)
        acquired = int(summary["acquired_count"])
        skipped = int(summary["skipped_count"])
        missing = int(summary["missing_recommendation_count"])
        if acquired == 0:
            state = STATE_NO_ACQUISITION
            result = "flow_no_acquisition_available"
            message = "No recommendation in the batch could be acquired."
        elif acquired == BATCH_TARGET_COUNT and skipped == 0 and missing == 0:
            state = STATE_COMPLETED
            result = "flow_completed"
            message = "All three recommended movies were transferred and recorded."
        else:
            state = STATE_COMPLETED
            result = "flow_completed_with_skips"
            message = (
                f"The batch acquired {acquired} movie(s); {skipped + missing} were unavailable."
            )
        manifest.update(
            {
                "state": state,
                "result": result,
                "current_stage": "history",
                "current_item_rank": None,
                "completed_at": self._timestamp(),
                "message": message,
                "attention": None,
                "retryable_failure": None,
                "allowed_actions": ["status"],
            }
        )
        self._refresh_aggregate_item_stages(manifest)
        self._write_manifest(manifest)
        return manifest, EXIT_OK

    def _refresh_batch_summary(self, manifest: JsonObject) -> JsonObject:
        items = self._batch_items(manifest)
        acquired = sum(1 for item in items if item.get("status") == "ACQUIRED")
        skipped = sum(1 for item in items if item.get("status") == "SKIPPED")
        summary: JsonObject = {
            "target_count": BATCH_TARGET_COUNT,
            "requested_count": len(items),
            "acquired_count": acquired,
            "skipped_count": skipped,
            "missing_recommendation_count": max(0, BATCH_TARGET_COUNT - len(items)),
        }
        self._batch(manifest)["summary"] = summary
        manifest["summary"] = dict(summary)
        manifest["missing_recommendation_slots"] = list(
            range(len(items) + 1, BATCH_TARGET_COUNT + 1)
        )
        return summary

    def _refresh_aggregate_item_stages(self, manifest: JsonObject) -> None:
        items = self._batch_items(manifest)
        for key in ITEM_STAGE_KEYS:
            statuses = [self._stage_record_for(manifest, key, item).get("status") for item in items]
            aggregate = self._aggregate_stage_record(manifest, key)
            if any(status in {"FAILED", "INTERRUPTED_RETRYABLE"} for status in statuses):
                aggregate["status"] = "FAILED"
            elif statuses and all(status == "SUCCEEDED" for status in statuses):
                aggregate["status"] = "SUCCEEDED"
            elif any(status in {"SUCCEEDED", "PARTIAL_SUCCESS"} for status in statuses):
                aggregate["status"] = "PARTIAL_SUCCESS"
            elif statuses and all(status in {"NO_RESULT", "SKIPPED"} for status in statuses):
                aggregate["status"] = "NO_RESULT"

    def _advance(
        self, manifest: JsonObject, completed: StageSpec, *, completed_status: str = "SUCCEEDED"
    ) -> None:
        stage_record = self._stage_record(manifest, completed.key)
        stage_record["status"] = completed_status
        if completed.key == "download":
            manifest.pop("resume_stalled_authorized", None)
        next_stage = STAGES[completed.position]
        manifest.update(
            {
                "state": STATE_RUNNING,
                "result": "flow_running",
                "current_stage": next_stage.key,
                "message": f"Stage {completed.key} completed; {next_stage.key} is next.",
                "retryable_failure": None,
                "attention": None,
                "allowed_actions": ["status"],
            }
        )
        self._write_manifest(manifest)

    def _finish(
        self, manifest: JsonObject, *, state: str, result: str, message: str
    ) -> tuple[JsonObject, int]:
        current = self._stage_record(manifest, self._stage_key(manifest))
        current["status"] = "SUCCEEDED" if state != STATE_NO_ACQUISITION else "NO_RESULT"
        manifest.update(
            {
                "state": state,
                "result": result,
                "completed_at": self._timestamp(),
                "message": message,
                "attention": None,
                "retryable_failure": None,
                "allowed_actions": ["status"],
            }
        )
        self._write_manifest(manifest)
        return manifest, EXIT_OK

    def _new_manifest(self) -> JsonObject:
        timestamp = self._timestamp()
        run_id = self._new_run_id()
        run_directory = self.runs / run_id
        run_directory.mkdir(parents=False)
        manifest: JsonObject = {
            "schema_version": FLOW_SCHEMA_VERSION,
            "result": "flow_running",
            "run_id": run_id,
            "state": STATE_RUNNING,
            "current_stage": "recommendations",
            "created_at": timestamp,
            "updated_at": timestamp,
            "artifact_directory": str(run_directory),
            "message": "A new movie flow was created.",
            "stages": {
                stage.key: {"position": stage.position, "status": "PENDING", "attempts": []}
                for stage in STAGES
            },
            "attention": None,
            "retryable_failure": None,
            "allowed_actions": ["status"],
            "current_item_rank": None,
            "items": [],
            "summary": {
                "target_count": BATCH_TARGET_COUNT,
                "requested_count": 0,
                "acquired_count": 0,
                "skipped_count": 0,
                "missing_recommendation_count": BATCH_TARGET_COUNT,
            },
            "missing_recommendation_slots": [1, 2, 3],
            "batch": {
                "target_count": BATCH_TARGET_COUNT,
                "summary": {
                    "target_count": BATCH_TARGET_COUNT,
                    "requested_count": 0,
                    "acquired_count": 0,
                    "skipped_count": 0,
                    "missing_recommendation_count": BATCH_TARGET_COUNT,
                },
            },
        }
        self._write_manifest(manifest)
        return manifest

    def _input_artifact(self, manifest: JsonObject, stage: StageSpec) -> Path | None:
        if stage.position == 1:
            return None
        preceding = STAGES[stage.position - 2]
        record = self._stage_record(manifest, preceding.key)
        raw_path = record.get("latest_artifact")
        if not isinstance(raw_path, str) or not raw_path:
            raise FlowManifestError(
                f"Stage {stage.key} cannot start because {preceding.key} has no artifact."
            )
        path = Path(raw_path)
        return self._validated_artifact_path(manifest, path)

    def _batch_input_artifact(
        self, manifest: JsonObject, stage: StageSpec, item: JsonObject | None
    ) -> Path | None:
        if stage.key == "recommendations":
            return None
        if stage.key == "search":
            record = self._aggregate_stage_record(manifest, "recommendations")
        elif stage.key == "probe":
            record = self._aggregate_stage_record(manifest, "search")
        else:
            if item is None:
                raise FlowManifestError(f"Batch stage {stage.key} has no current item.")
            previous = {
                "download": "probe",
                "transfer": "download",
                "history": "transfer",
            }[stage.key]
            record = self._stage_record_for(manifest, previous, item)
        raw_path = record.get("latest_artifact")
        if not isinstance(raw_path, str) or not raw_path:
            raise FlowManifestError(
                f"Stage {stage.key} cannot start because its input artifact is missing."
            )
        return self._validated_artifact_path(manifest, Path(raw_path))

    def _stage_record(self, manifest: JsonObject, stage_key: str) -> JsonObject:
        stages = manifest.get("stages")
        if not isinstance(stages, dict):
            raise FlowManifestError("The flow manifest has no valid stages object.")
        record = stages.get(stage_key)
        if not isinstance(record, dict):
            raise FlowManifestError(f"The flow manifest has no valid {stage_key} stage.")
        return record

    def _aggregate_stage_record(self, manifest: JsonObject, stage_key: str) -> JsonObject:
        return self._stage_record(manifest, stage_key)

    def _stage_record_for(
        self, manifest: JsonObject, stage_key: str, item: JsonObject | None
    ) -> JsonObject:
        if item is None or not self._is_batch_manifest(manifest) or stage_key in SHARED_STAGE_KEYS:
            return self._stage_record(manifest, stage_key)
        stages = item.get("stages")
        if not isinstance(stages, dict):
            raise FlowManifestError("The current batch item has no valid stages object.")
        record = stages.get(stage_key)
        if not isinstance(record, dict):
            raise FlowManifestError(f"The current batch item has no valid {stage_key} stage.")
        return record

    def _is_batch_manifest(self, manifest: JsonObject) -> bool:
        return manifest.get("schema_version") == FLOW_SCHEMA_VERSION

    def _batch(self, manifest: JsonObject) -> JsonObject:
        batch = manifest.get("batch")
        if not isinstance(batch, dict):
            raise FlowManifestError("The batch flow manifest has no valid batch object.")
        return batch

    def _batch_items(self, manifest: JsonObject) -> list[JsonObject]:
        items = manifest.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise FlowManifestError("The batch flow manifest has no valid items array.")
        return items  # type: ignore[return-value]

    def _current_item(self, manifest: JsonObject) -> JsonObject:
        rank = manifest.get("current_item_rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            raise FlowManifestError("The batch flow manifest has no current item.")
        for item in self._batch_items(manifest):
            if item.get("recommendation_rank") == rank:
                return item
        raise FlowManifestError("The batch flow manifest current item does not exist.")

    def _cleanup_clear_continues(self, manifest: JsonObject) -> bool:
        attention = manifest.get("attention")
        return isinstance(attention, dict) and attention.get("clear_continues_batch") is True

    def _stage_key(self, manifest: JsonObject) -> str:
        value = manifest.get("current_stage")
        if not isinstance(value, str) or value not in STAGE_BY_KEY:
            raise FlowManifestError("The flow manifest has an invalid current_stage.")
        return value

    def _latest_error_code(self, manifest: JsonObject) -> str:
        attention = manifest.get("attention")
        if isinstance(attention, dict):
            value = attention.get("error_code")
            return value if isinstance(value, str) else ""
        return ""

    def _resume_allowed(self, manifest: JsonObject) -> bool:
        attention = manifest.get("attention")
        return isinstance(attention, dict) and attention.get("resume_allowed") is True

    def _resume_denied_message(self, manifest: JsonObject) -> str:
        stage = manifest.get("current_stage")
        error_code = self._latest_error_code(manifest)
        if stage == "probe":
            return "An interrupted or unclean probe must be inspected and cleared manually."
        if error_code == "SEEDBOX_CLEANUP_FAILED" or self._cleanup_clear_continues(manifest):
            return (
                "A completed transfer with failed cleanup cannot be retransferred; "
                "clean up and clear it."
            )
        return "This attention state cannot be resumed safely; inspect it and use clear."

    def _attention_guidance(self, stage: str, error_code: str, resume_allowed: bool) -> str:
        if stage == "probe":
            return (
                "Inspect rTorrent and the probe directory, clean up any retained probe, then clear."
            )
        if stage == "transfer" and error_code == "SEEDBOX_CLEANUP_FAILED":
            return (
                "The local transfer finished. Clean the reported seedbox paths manually, "
                "then clear."
            )
        if resume_allowed:
            return "Correct the reported condition, then run the resume command for this run ID."
        return "Inspect local and remote state, then clear the run after resolving it manually."

    def _load_current_run(self, run_id: str) -> JsonObject:
        latest = self._load_latest(optional=False)
        if latest.get("run_id") != run_id:
            raise FlowManifestError("Only the latest blocking run can be changed.")
        return latest

    def _load_latest(self, *, optional: bool) -> JsonObject | None:
        candidates = list(self.runs.glob("*/manifest.json")) if self.runs.is_dir() else []
        if candidates:
            try:
                newest = max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))
            except OSError as exc:
                raise FlowManifestError("Could not inspect movie-flow run manifests.") from exc
            return self._read_manifest(newest)
        if not self.latest.exists():
            if optional:
                return None
            raise FlowManifestError("No latest movie-flow run exists.")
        return self._read_manifest(self.latest)

    def _load_run(self, run_id: str) -> JsonObject:
        if not _valid_run_id(run_id):
            raise FlowManifestError("The requested run ID is invalid.")
        return self._read_manifest(self.runs / run_id / "manifest.json", expected_run_id=run_id)

    def _read_manifest(self, path: Path, *, expected_run_id: str | None = None) -> JsonObject:
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FlowManifestError(f"Movie-flow manifest was not found: {path}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FlowManifestError(f"Movie-flow manifest is not readable JSON: {path}") from exc
        if not isinstance(decoded, dict) or decoded.get("schema_version") not in {
            LEGACY_FLOW_SCHEMA_VERSION,
            FLOW_SCHEMA_VERSION,
        }:
            raise FlowManifestError(f"Movie-flow manifest has an unsupported schema: {path}")
        run_id = decoded.get("run_id")
        if not isinstance(run_id, str) or not _valid_run_id(run_id):
            raise FlowManifestError(f"Movie-flow manifest has an invalid run ID: {path}")
        if expected_run_id is not None and run_id != expected_run_id:
            raise FlowManifestError("The requested run ID does not match its manifest.")
        stages = decoded.get("stages")
        if isinstance(stages, dict) and "history" not in stages:
            stages["history"] = {"position": 6, "status": "PENDING", "attempts": []}
        self._stage_key(decoded)
        return decoded

    def _read_stage_artifact(self, manifest: JsonObject, path: Path) -> JsonObject:
        path = self._validated_artifact_path(manifest, path)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FlowManifestError(
                f"Persisted stage artifact is not readable JSON: {path}"
            ) from exc
        if not isinstance(decoded, dict):
            raise FlowManifestError(f"Persisted stage artifact is not a JSON object: {path}")
        return decoded

    def _validated_artifact_path(self, manifest: JsonObject, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._run_directory(manifest).resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise FlowManifestError(
                "A required stage artifact is missing or outside its run directory."
            ) from exc
        if not resolved.is_file():
            raise FlowManifestError(f"Required stage artifact is not a file: {resolved}")
        return resolved

    def _write_manifest(self, manifest: JsonObject) -> None:
        manifest["updated_at"] = self._timestamp()
        run_manifest = self._run_directory(manifest) / "manifest.json"
        _write_json_atomic(run_manifest, manifest)
        _write_json_atomic(self.latest, manifest)

    def _run_directory(self, manifest: JsonObject) -> Path:
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not _valid_run_id(run_id):
            raise FlowManifestError("The flow manifest has an invalid run ID.")
        path = self.runs / run_id
        try:
            path.resolve().relative_to(self.runs.resolve())
        except ValueError as exc:
            raise FlowManifestError("The flow run directory escapes the configured root.") from exc
        return path

    def _prepare_storage(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FlowConfigurationError("MOVIE_FLOW_DIRECTORY is not a directory.")
        self.runs.mkdir(exist_ok=True)

    def _new_run_id(self) -> str:
        stamp = self.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"movie-flow-{stamp}-{uuid.uuid4().hex[:8]}"

    def _timestamp(self) -> str:
        return self.now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _already_running(self) -> JsonObject:
        return {
            "schema_version": FLOW_SCHEMA_VERSION,
            "result": "flow_already_running",
            "message": "Another movie-flow process currently holds the orchestration lock.",
        }


class FlowLock:
    """Small non-blocking advisory file lock for Windows and POSIX systems."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        self.acquired = True
        return True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        if self.acquired:
            self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self.handle.close()


def run_process(
    command: list[str],
    input_path: Path | None,
    stdout_path: Path,
    stderr_path: Path,
    working_directory: Path,
) -> int:
    """Run one child module with file-backed standard streams."""
    with ExitStack() as stack:
        stdin: int | BinaryIO
        if input_path is None:
            stdin = subprocess.DEVNULL
        else:
            stdin = stack.enter_context(input_path.open("rb"))
        stdout = stack.enter_context(stdout_path.open("wb"))
        stderr = stack.enter_context(stderr_path.open("wb"))
        completed = subprocess.run(  # noqa: S603 - command is a fixed internal module list
            command,
            cwd=working_directory,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    return completed.returncode


def resolve_flow_root(*, create: bool) -> Path:
    """Resolve configured orchestration storage without exposing configuration values."""
    explicit = os.getenv("MOVIE_FLOW_DIRECTORY", "").strip()
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root
    recommendations = os.getenv("RECOMMENDATIONS_DIRECTORY", "").strip()
    if not recommendations:
        raise FlowConfigurationError(
            "Set MOVIE_FLOW_DIRECTORY or configure RECOMMENDATIONS_DIRECTORY."
        )
    base = Path(recommendations).expanduser().resolve()
    if not base.is_dir():
        raise FlowConfigurationError(
            "RECOMMENDATIONS_DIRECTORY must exist when MOVIE_FLOW_DIRECTORY is not set."
        )
    root = base / "movie-flow"
    if create:
        root.mkdir(exist_ok=True)
    return root


def _write_json_atomic(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialize_json(payload, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _valid_run_id(value: str) -> bool:
    return value.startswith("movie-flow-") and all(
        character.isalnum() or character == "-" for character in value
    )


def _safe_os_message(exc: OSError) -> str:
    return exc.strerror or "An operating-system error occurred."


def _error_payload(error_code: str, message: str) -> JsonObject:
    return {
        "schema_version": FLOW_SCHEMA_VERSION,
        "result": "flow_error",
        "error_code": error_code,
        "message": message,
    }


def _add_pretty(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output JSON.")


if __name__ == "__main__":
    raise SystemExit(main())
