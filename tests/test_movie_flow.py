"""Tests for checkpointed movie-flow orchestration."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_scope.movie_flow import (
    EXIT_ATTENTION,
    EXIT_OK,
    EXIT_RETRYABLE,
    STATE_ATTENTION,
    STATE_CLEARED,
    STATE_COMPLETED,
    STATE_NO_ACQUISITION,
    STATE_NO_ACTION,
    STATE_RETRYABLE,
    FlowLock,
    FlowManifestError,
    MovieFlowOrchestrator,
    main,
)


class FakeRunner:
    """Queue deterministic subprocess outcomes by module name."""

    def __init__(self) -> None:
        self.outcomes: dict[str, deque[tuple[int, object]]] = defaultdict(deque)
        self.calls: list[dict[str, Any]] = []

    def add(self, module: str, exit_code: int, payload: object) -> None:
        self.outcomes[module].append((exit_code, payload))

    def __call__(
        self,
        command: list[str],
        input_path: Path | None,
        stdout_path: Path,
        stderr_path: Path,
        working_directory: Path,
    ) -> int:
        module = command[command.index("-m") + 1]
        input_text = input_path.read_text(encoding="utf-8") if input_path else None
        self.calls.append(
            {
                "module": module,
                "command": command,
                "input": input_text,
                "working_directory": working_directory,
            }
        )
        if module == "media_scope.movie_history" and not self.outcomes[module]:
            self.add(module, 0, history_payload())
        exit_code, outcome = self.outcomes[module].popleft()
        stderr_path.write_text(f"log for {module}\n", encoding="utf-8")
        if isinstance(outcome, BaseException):
            raise outcome
        text = outcome if isinstance(outcome, str) else json.dumps(outcome)
        stdout_path.write_text(text, encoding="utf-8")
        return exit_code


def recommendation_payload(result: str = "recommendations_created") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": result,
        "recommendations": (
            [{"tmdb_id": 1, "title": "Movie", "year": 2000}]
            if result == "recommendations_created"
            else []
        ),
    }


def search_payload() -> dict[str, Any]:
    return {"schema_version": 1, "result": "movie_search_completed", "movies": []}


def probe_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "candidate_health_validated",
        "selected_candidate": {"status": "READY_FOR_DOWNLOAD"},
    }


def download_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "download_completed",
        "status": "READY_FOR_TRANSFER",
    }


def transfer_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "transfer_completed",
        "scope": {"media_type": "movie", "tmdb_id": 1, "title": "Movie", "year": 2000},
        "transfer": {"status": "COMPLETED"},
    }


def history_payload(result: str = "movie_history_recorded") -> dict[str, Any]:
    return {"schema_version": 1, "result": result}


def add_happy_path(runner: FakeRunner) -> None:
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())


def orchestrator(tmp_path: Path, runner: FakeRunner) -> MovieFlowOrchestrator:
    return MovieFlowOrchestrator(
        tmp_path / "flow",
        process_runner=runner,
        now=lambda: datetime(2026, 8, 9, 16, 0, tzinfo=UTC),
        working_directory=tmp_path,
    )


def test_happy_path_persists_exact_handoffs_logs_and_manifest(tmp_path: Path) -> None:
    runner = FakeRunner()
    add_happy_path(runner)

    payload, code = orchestrator(tmp_path, runner).run()

    assert code == EXIT_OK
    assert payload["state"] == STATE_COMPLETED
    assert [call["module"] for call in runner.calls] == [
        "media_scope.recommend_cli",
        "media_scope.movie_search",
        "media_scope.movie_probe",
        "media_scope.download_torrent",
        "media_scope.movie_transfer",
        "media_scope.movie_history",
    ]
    assert json.loads(runner.calls[1]["input"]) == recommendation_payload()
    assert json.loads(runner.calls[2]["input"]) == search_payload()
    assert json.loads(runner.calls[3]["input"]) == probe_payload()
    assert json.loads(runner.calls[4]["input"]) == download_payload()
    assert json.loads(runner.calls[5]["input"]) == transfer_payload()

    run_directory = Path(payload["artifact_directory"])
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "flow" / "latest-status.json").read_text(encoding="utf-8"))
    assert manifest == latest == payload
    assert len(list(run_directory.glob("*.json"))) == 7
    assert len(list(run_directory.glob("*.stderr.log"))) == 6
    assert not list(run_directory.glob("*.partial"))


def test_recommendations_not_needed_stops_without_starting_search(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add(
        "media_scope.recommend_cli",
        0,
        recommendation_payload("recommendations_not_needed"),
    )

    payload, code = orchestrator(tmp_path, runner).run()

    assert (code, payload["state"]) == (EXIT_OK, STATE_NO_ACTION)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("exit_code", "result"),
    [(3, "NO_PROBEABLE_CANDIDATES"), (6, "NO_HEALTHY_TORRENT_FOUND")],
)
def test_no_candidate_is_a_nonblocking_terminal_run(
    tmp_path: Path, exit_code: int, result: str
) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add(
        "media_scope.movie_probe",
        exit_code,
        {"schema_version": 1, "result": result, "error_code": result},
    )

    payload, code = orchestrator(tmp_path, runner).run()

    assert (code, payload["state"]) == (EXIT_OK, STATE_NO_ACQUISITION)


def test_retryable_search_continues_without_repeating_recommendations(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add(
        "media_scope.movie_search",
        4,
        {"schema_version": 1, "result": "error", "error_code": "JACKETT_UNAVAILABLE"},
    )
    flow = orchestrator(tmp_path, runner)

    first, first_code = flow.run()
    assert (first_code, first["state"]) == (EXIT_RETRYABLE, STATE_RETRYABLE)

    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())
    second, second_code = flow.run()

    assert (second_code, second["state"]) == (EXIT_OK, STATE_COMPLETED)
    assert [call["module"] for call in runner.calls].count("media_scope.recommend_cli") == 1
    assert [call["module"] for call in runner.calls].count("media_scope.movie_search") == 2


def test_interrupted_download_is_replayed_from_probe_artifact(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add("media_scope.download_torrent", 1, RuntimeError("simulated reboot"))
    flow = orchestrator(tmp_path, runner)

    with pytest.raises(RuntimeError, match="simulated reboot"):
        flow.run()

    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())
    payload, code = flow.run()

    assert (code, payload["state"]) == (EXIT_OK, STATE_COMPLETED)
    download_calls = [
        call for call in runner.calls if call["module"] == "media_scope.download_torrent"
    ]
    assert len(download_calls) == 2
    assert json.loads(download_calls[1]["input"]) == probe_payload()


@pytest.mark.parametrize(
    ("interrupted_module", "earlier_outcomes"),
    [
        ("media_scope.recommend_cli", []),
        ("media_scope.movie_search", [("media_scope.recommend_cli", recommendation_payload())]),
    ],
)
def test_interrupted_early_stage_is_replayed(
    tmp_path: Path,
    interrupted_module: str,
    earlier_outcomes: list[tuple[str, dict[str, Any]]],
) -> None:
    runner = FakeRunner()
    for module, payload in earlier_outcomes:
        runner.add(module, 0, payload)
    runner.add(interrupted_module, 1, RuntimeError("simulated reboot"))
    flow = orchestrator(tmp_path, runner)

    with pytest.raises(RuntimeError, match="simulated reboot"):
        flow.run()

    if interrupted_module == "media_scope.recommend_cli":
        runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())
    payload, code = flow.run()

    assert (code, payload["state"]) == (EXIT_OK, STATE_COMPLETED)
    assert [call["module"] for call in runner.calls].count(interrupted_module) == 2


@pytest.mark.parametrize("module", ["media_scope.movie_probe", "media_scope.movie_transfer"])
def test_interrupted_externally_ambiguous_stage_requires_attention(
    tmp_path: Path, module: str
) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    if module == "media_scope.movie_probe":
        runner.add(module, 1, RuntimeError("simulated reboot"))
    else:
        runner.add("media_scope.movie_probe", 0, probe_payload())
        runner.add("media_scope.download_torrent", 0, download_payload())
        runner.add(module, 1, RuntimeError("simulated reboot"))
    flow = orchestrator(tmp_path, runner)

    with pytest.raises(RuntimeError, match="simulated reboot"):
        flow.run()
    payload, code = flow.run()

    assert (code, payload["state"]) == (EXIT_ATTENTION, STATE_ATTENTION)
    assert payload["attention"]["error_code"] == "INTERRUPTED_STAGE"


def test_completed_transfer_artifact_is_evaluated_after_crash_without_retransfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    add_happy_path(runner)
    flow = orchestrator(tmp_path, runner)
    original_evaluate = flow._evaluate_stage

    def crash_after_transfer(*args: Any, **kwargs: Any) -> Any:
        stage = args[1]
        if stage.key == "transfer":
            raise RuntimeError("crash after transfer output")
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(flow, "_evaluate_stage", crash_after_transfer)
    with pytest.raises(RuntimeError, match="crash after transfer output"):
        flow.run()

    recovery_runner = FakeRunner()
    recovered, code = orchestrator(tmp_path, recovery_runner).run()

    assert (code, recovered["state"]) == (EXIT_OK, STATE_COMPLETED)
    assert [call["module"] for call in recovery_runner.calls] == ["media_scope.movie_history"]


def test_stalled_download_resume_forwards_resume_stalled(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add(
        "media_scope.download_torrent",
        6,
        {
            "schema_version": 1,
            "result": "download_failed",
            "error_code": "DOWNLOAD_STALLED",
        },
    )
    flow = orchestrator(tmp_path, runner)
    blocked, blocked_code = flow.run()
    assert blocked_code == EXIT_ATTENTION

    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())
    payload, code = flow.resume(str(blocked["run_id"]))

    assert (code, payload["state"]) == (EXIT_OK, STATE_COMPLETED)
    resumed = [call for call in runner.calls if call["module"] == "media_scope.download_torrent"][
        -1
    ]
    assert "--resume-stalled" in resumed["command"]


def test_authorized_stalled_resume_survives_retryable_connection_failure(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add(
        "media_scope.download_torrent",
        6,
        {
            "schema_version": 1,
            "result": "download_failed",
            "error_code": "DOWNLOAD_STALLED",
        },
    )
    flow = orchestrator(tmp_path, runner)
    blocked, _ = flow.run()
    runner.add(
        "media_scope.download_torrent",
        4,
        {
            "schema_version": 1,
            "result": "download_failed",
            "error_code": "RTORRENT_CONNECTION_FAILED",
        },
    )

    retryable, retryable_code = flow.resume(str(blocked["run_id"]))
    assert (retryable_code, retryable["state"]) == (EXIT_RETRYABLE, STATE_RETRYABLE)

    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add("media_scope.movie_transfer", 0, transfer_payload())
    payload, code = flow.run()

    assert (code, payload["state"]) == (EXIT_OK, STATE_COMPLETED)
    download_calls = [
        call for call in runner.calls if call["module"] == "media_scope.download_torrent"
    ]
    assert all("--resume-stalled" in call["command"] for call in download_calls[1:])


def test_cleanup_failure_cannot_resume_and_clear_is_non_destructive(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload())
    runner.add("media_scope.download_torrent", 0, download_payload())
    runner.add(
        "media_scope.movie_transfer",
        7,
        {
            "schema_version": 1,
            "result": "transfer_completed_cleanup_failed",
            "error_code": "SEEDBOX_CLEANUP_FAILED",
            "message": "cleanup failed",
            "scope": {
                "media_type": "movie",
                "tmdb_id": 1,
                "title": "Movie",
                "year": 2000,
            },
            "transfer": {"status": "COMPLETED"},
        },
    )
    flow = orchestrator(tmp_path, runner)
    blocked, code = flow.run()

    assert code == EXIT_ATTENTION
    assert [call["module"] for call in runner.calls][-2:] == [
        "media_scope.movie_transfer",
        "media_scope.movie_history",
    ]
    assert blocked["stages"]["history"]["status"] == "SUCCEEDED"
    with pytest.raises(FlowManifestError, match="cannot be retransferred"):
        flow.resume(str(blocked["run_id"]))

    cleared, clear_code = flow.clear(str(blocked["run_id"]), "Cleaned seedbox manually")
    assert (clear_code, cleared["state"]) == (EXIT_OK, STATE_CLEARED)
    assert Path(cleared["artifact_directory"]).is_dir()
    assert cleared["clear_reason"] == "Cleaned seedbox manually"


def test_history_failure_retries_only_history_without_retransfer(tmp_path: Path) -> None:
    runner = FakeRunner()
    add_happy_path(runner)
    runner.add(
        "media_scope.movie_history",
        4,
        {
            "schema_version": 1,
            "result": "movie_history_failed",
            "error_code": "GOOGLE_SHEET_REQUEST_FAILED",
            "message": "The Google Sheet could not be updated.",
        },
    )
    flow = orchestrator(tmp_path, runner)

    blocked, blocked_code = flow.run()
    assert (blocked_code, blocked["state"], blocked["current_stage"]) == (
        EXIT_RETRYABLE,
        STATE_RETRYABLE,
        "history",
    )

    runner.add("media_scope.movie_history", 0, history_payload("movie_history_already_recorded"))
    completed, completed_code = flow.run()

    assert (completed_code, completed["state"]) == (EXIT_OK, STATE_COMPLETED)
    modules = [call["module"] for call in runner.calls]
    assert modules.count("media_scope.movie_transfer") == 1
    assert modules.count("media_scope.movie_history") == 2


def test_interrupted_history_is_replayed_without_retransfer(tmp_path: Path) -> None:
    runner = FakeRunner()
    add_happy_path(runner)
    runner.add("media_scope.movie_history", 1, RuntimeError("simulated reboot"))
    flow = orchestrator(tmp_path, runner)

    with pytest.raises(RuntimeError, match="simulated reboot"):
        flow.run()
    completed, code = flow.run()

    assert (code, completed["state"]) == (EXIT_OK, STATE_COMPLETED)
    modules = [call["module"] for call in runner.calls]
    assert modules.count("media_scope.movie_transfer") == 1
    assert modules.count("media_scope.movie_history") == 2


def test_invalid_probe_output_requires_attention_and_keeps_partial_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 8, "not json")

    payload, code = orchestrator(tmp_path, runner).run()

    assert (code, payload["state"]) == (EXIT_ATTENTION, STATE_ATTENTION)
    assert list(Path(payload["artifact_directory"]).glob("*.partial"))


def test_lock_contention_is_a_successful_noop(tmp_path: Path) -> None:
    runner = FakeRunner()
    flow = orchestrator(tmp_path, runner)
    flow._prepare_storage()

    with FlowLock(flow.lock_path) as acquired:
        assert acquired
        payload, code = flow.run()

    assert code == EXIT_OK
    assert payload["result"] == "flow_already_running"
    assert not runner.calls


def test_child_launch_failure_is_retryable_even_for_mutating_stage(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 1, OSError(2, "executable unavailable"))

    payload, code = orchestrator(tmp_path, runner).run()

    assert (code, payload["state"]) == (EXIT_RETRYABLE, STATE_RETRYABLE)


def test_main_reports_corrupt_latest_manifest_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "flow"
    root.mkdir()
    (root / "latest-status.json").write_text("not json", encoding="utf-8")
    monkeypatch.setenv("MOVIE_FLOW_DIRECTORY", str(root))

    code = main(["status"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["result"] == "flow_error"
    assert payload["error_code"] == "FLOW_CONFIGURATION_ERROR"
