"""Focused tests for schema-v2 sequential movie-flow batches.

These tests deliberately use the same file-backed subprocess seam as
``tests/test_movie_flow.py``.  The runner records every handoff and routes
outcomes by recommendation rank, which makes it possible to prove that a
later recommendation is not hidden behind the first one.
"""

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
    STATE_RETRYABLE,
    MovieFlowOrchestrator,
)

MOVIES = (
    {"tmdb_id": 101, "title": "First Movie", "year": 2001},
    {"tmdb_id": 202, "title": "Second Movie", "year": 2002},
    {"tmdb_id": 303, "title": "Third Movie", "year": 2003},
)


class BatchRunner:
    """Queue deterministic child outcomes, optionally scoped to one rank."""

    def __init__(self) -> None:
        self.outcomes: dict[tuple[str, int | None], deque[tuple[int, object]]] = defaultdict(deque)
        self.calls: list[dict[str, Any]] = []

    def add(self, module: str, exit_code: int, payload: object, *, rank: int | None = None) -> None:
        self.outcomes[(module, rank)].append((exit_code, payload))

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
        rank = _command_rank(command, input_text)
        self.calls.append(
            {
                "module": module,
                "rank": rank,
                "command": command,
                "input": input_text,
                "working_directory": working_directory,
            }
        )
        key = (module, rank)
        if not self.outcomes[key]:
            key = (module, None)
        if not self.outcomes[key]:
            if module == "media_scope.movie_history":
                self.add(module, 0, history_payload())
            else:
                raise AssertionError(f"No queued outcome for {module} rank={rank}")
        exit_code, outcome = self.outcomes[key].popleft()
        stderr_path.write_text(f"log for {module} rank={rank}\n", encoding="utf-8")
        if isinstance(outcome, BaseException):
            raise outcome
        stdout_path.write_text(
            outcome if isinstance(outcome, str) else json.dumps(outcome), encoding="utf-8"
        )
        return exit_code


def recommendation_payload(movies: tuple[dict[str, Any], ...] = MOVIES) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "recommendations_created",
        "recommendations": list(movies),
    }


def search_payload(movies: tuple[dict[str, Any], ...] = MOVIES) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "movie_search_completed",
        "movies": [
            {
                "recommendation": movie,
                "results": [
                    {
                        "rank": 1,
                        "title": f"{movie['title']} Release",
                        "reported_seeders": 10,
                        "infohash": f"{movie['tmdb_id']:040x}",
                    }
                ],
            }
            for movie in movies
        ],
    }


def probe_payload(rank: int) -> dict[str, Any]:
    movie = MOVIES[rank - 1]
    return {
        "schema_version": 1,
        "result": "candidate_health_validated",
        "scope": {"media_type": "movie", **movie, "recommendation_rank": rank},
        "selected_candidate": {
            "status": "READY_FOR_DOWNLOAD",
            "recommendation_rank": rank,
        },
    }


def download_payload(rank: int) -> dict[str, Any]:
    movie = MOVIES[rank - 1]
    return {
        "schema_version": 1,
        "result": "download_completed",
        "status": "READY_FOR_TRANSFER",
        "scope": {"media_type": "movie", **movie, "recommendation_rank": rank},
    }


def transfer_payload(
    rank: int,
    *,
    cleanup_failed: bool = False,
    cleanup_error_code: str = "SEEDBOX_CLEANUP_FAILED",
) -> dict[str, Any]:
    movie = MOVIES[rank - 1]
    if cleanup_failed:
        return {
            "schema_version": 1,
            "result": "transfer_completed_cleanup_failed",
            "error_code": cleanup_error_code,
            "scope": {"media_type": "movie", **movie, "recommendation_rank": rank},
            "transfer": {"status": "COMPLETED"},
        }
    return {
        "schema_version": 1,
        "result": "transfer_completed",
        "scope": {"media_type": "movie", **movie, "recommendation_rank": rank},
        "transfer": {"status": "COMPLETED"},
    }


def history_payload(result: str = "movie_history_recorded") -> dict[str, Any]:
    return {"schema_version": 1, "result": result}


def happy_batch(runner: BatchRunner, movies: tuple[dict[str, Any], ...] = MOVIES) -> None:
    runner.add("media_scope.recommend_cli", 0, recommendation_payload(movies))
    runner.add("media_scope.movie_search", 0, search_payload(movies))
    for rank in range(1, len(movies) + 1):
        runner.add("media_scope.movie_probe", 0, probe_payload(rank), rank=rank)
        runner.add("media_scope.download_torrent", 0, download_payload(rank), rank=rank)
        runner.add("media_scope.movie_transfer", 0, transfer_payload(rank), rank=rank)
        runner.add("media_scope.movie_history", 0, history_payload(), rank=rank)


def flow(tmp_path: Path, runner: BatchRunner) -> MovieFlowOrchestrator:
    return MovieFlowOrchestrator(
        tmp_path / "flow",
        process_runner=runner,
        now=lambda: datetime(2026, 8, 9, 16, 0, tzinfo=UTC),
        working_directory=tmp_path,
    )


def _command_rank(command: list[str], input_text: str | None) -> int | None:
    if "--recommendation-rank" in command:
        return int(command[command.index("--recommendation-rank") + 1])
    for argument in command:
        if argument.startswith("--recommendation-rank="):
            return int(argument.partition("=")[2])
    if input_text:
        value = json.loads(input_text)
        scope = value.get("scope") if isinstance(value, dict) else None
        if isinstance(scope, dict) and isinstance(scope.get("recommendation_rank"), int):
            return int(scope["recommendation_rank"])
    return None


def _item_status(item: dict[str, Any]) -> str:
    value = item.get("status", item.get("state"))
    assert isinstance(value, str), item
    return value


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("items")
    assert isinstance(value, list), payload
    assert all(isinstance(item, dict) for item in value), value
    return value  # type: ignore[return-value]


def test_batch_happy_path_runs_all_three_in_order_and_persists_handoffs(tmp_path: Path) -> None:
    runner = BatchRunner()
    happy_batch(runner)

    payload, code = flow(tmp_path, runner).run()

    assert (code, payload["schema_version"], payload["result"]) == (
        EXIT_OK,
        2,
        "flow_completed",
    )
    modules = [call["module"] for call in runner.calls]
    assert modules == [
        "media_scope.recommend_cli",
        "media_scope.movie_search",
        "media_scope.movie_probe",
        "media_scope.download_torrent",
        "media_scope.movie_transfer",
        "media_scope.movie_history",
        "media_scope.movie_probe",
        "media_scope.download_torrent",
        "media_scope.movie_transfer",
        "media_scope.movie_history",
        "media_scope.movie_probe",
        "media_scope.download_torrent",
        "media_scope.movie_transfer",
        "media_scope.movie_history",
    ]
    assert [
        call["rank"] for call in runner.calls if call["module"] == "media_scope.movie_probe"
    ] == [1, 2, 3]
    assert modules.count("media_scope.recommend_cli") == 1
    assert modules.count("media_scope.movie_search") == 1
    items = _items(payload)
    assert [_item_status(item) for item in items] == ["ACQUIRED", "ACQUIRED", "ACQUIRED"]
    assert [item["recommendation_rank"] for item in items] == [1, 2, 3]
    latest = json.loads((tmp_path / "flow" / "latest-status.json").read_text(encoding="utf-8"))
    assert latest == payload


@pytest.mark.parametrize("unavailable_rank", [1, 2, 3])
def test_unavailable_item_is_skipped_and_later_items_continue(
    tmp_path: Path, unavailable_rank: int
) -> None:
    runner = BatchRunner()
    happy_batch(runner)
    runner.outcomes[("media_scope.movie_probe", unavailable_rank)].clear()
    runner.add(
        "media_scope.movie_probe",
        3,
        {
            "schema_version": 1,
            "result": "NO_PROBEABLE_CANDIDATES",
            "error_code": "NO_PROBEABLE_CANDIDATES",
        },
        rank=unavailable_rank,
    )

    payload, code = flow(tmp_path, runner).run()

    assert (code, payload["result"]) == (EXIT_OK, "flow_completed_with_skips")
    assert [_item_status(item) for item in _items(payload)][unavailable_rank - 1] == "SKIPPED"
    assert len([call for call in runner.calls if call["module"] == "media_scope.movie_probe"]) == 3
    assert [
        call["rank"] for call in runner.calls if call["module"] == "media_scope.movie_transfer"
    ] == [rank for rank in range(1, 4) if rank != unavailable_rank]


def test_all_unavailable_is_no_acquisition_available(tmp_path: Path) -> None:
    runner = BatchRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    for rank in range(1, 4):
        runner.add(
            "media_scope.movie_probe",
            6,
            {
                "schema_version": 1,
                "result": "NO_HEALTHY_TORRENT_FOUND",
                "error_code": "NO_HEALTHY_TORRENT_FOUND",
            },
            rank=rank,
        )

    payload, code = flow(tmp_path, runner).run()

    assert (code, payload["result"]) == (EXIT_OK, "flow_no_acquisition_available")
    assert all(_item_status(item) == "SKIPPED" for item in _items(payload))
    assert not [call for call in runner.calls if call["module"] == "media_scope.download_torrent"]


def test_fewer_recommendations_reports_missing_slots_in_aggregate(tmp_path: Path) -> None:
    movies = MOVIES[:2]
    runner = BatchRunner()
    happy_batch(runner, movies)

    payload, code = flow(tmp_path, runner).run()

    assert (code, payload["result"]) == (EXIT_OK, "flow_completed_with_skips")
    assert len(_items(payload)) == 2
    assert payload.get("missing_recommendation_slots", payload.get("missing_slots")) == [3]


def test_retryable_failure_resumes_current_item_without_replaying_completed_items(
    tmp_path: Path,
) -> None:
    runner = BatchRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload(1), rank=1)
    runner.add("media_scope.download_torrent", 0, download_payload(1), rank=1)
    runner.add("media_scope.movie_transfer", 0, transfer_payload(1), rank=1)
    runner.add("media_scope.movie_history", 0, history_payload(), rank=1)
    runner.add("media_scope.movie_probe", 0, probe_payload(2), rank=2)
    runner.add(
        "media_scope.download_torrent",
        4,
        {
            "schema_version": 1,
            "result": "download_failed",
            "error_code": "RTORRENT_CONNECTION_FAILED",
        },
        rank=2,
    )
    first, first_code = flow(tmp_path, runner).run()
    assert (first_code, first["state"]) == (EXIT_RETRYABLE, STATE_RETRYABLE)

    runner.add("media_scope.download_torrent", 0, download_payload(2), rank=2)
    runner.add("media_scope.movie_transfer", 0, transfer_payload(2), rank=2)
    runner.add("media_scope.movie_history", 0, history_payload(), rank=2)
    runner.add("media_scope.movie_probe", 0, probe_payload(3), rank=3)
    runner.add("media_scope.download_torrent", 0, download_payload(3), rank=3)
    runner.add("media_scope.movie_transfer", 0, transfer_payload(3), rank=3)
    runner.add("media_scope.movie_history", 0, history_payload(), rank=3)
    completed, completed_code = flow(tmp_path, runner).run()

    assert (completed_code, completed["result"]) == (EXIT_OK, "flow_completed")
    assert [
        call["rank"] for call in runner.calls if call["module"] == "media_scope.movie_probe"
    ] == [1, 2, 3]
    assert [
        call["rank"] for call in runner.calls if call["module"] == "media_scope.movie_transfer"
    ] == [1, 2, 3]
    assert (
        len(
            [
                call
                for call in runner.calls
                if call["module"] == "media_scope.download_torrent" and call["rank"] == 1
            ]
        )
        == 1
    )


def test_clear_cleanup_acknowledgment_continues_without_retransfer(tmp_path: Path) -> None:
    runner = BatchRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload())
    runner.add("media_scope.movie_search", 0, search_payload())
    runner.add("media_scope.movie_probe", 0, probe_payload(1), rank=1)
    runner.add("media_scope.download_torrent", 0, download_payload(1), rank=1)
    runner.add(
        "media_scope.movie_transfer",
        7,
        transfer_payload(
            1,
            cleanup_failed=True,
            cleanup_error_code="RTORRENT_RPC_UNAVAILABLE",
        ),
        rank=1,
    )
    runner.add("media_scope.movie_history", 0, history_payload(), rank=1)
    blocked, blocked_code = flow(tmp_path, runner).run()
    assert (blocked_code, blocked["state"]) == (EXIT_ATTENTION, STATE_ATTENTION)

    for rank in (2, 3):
        runner.add("media_scope.movie_probe", 0, probe_payload(rank), rank=rank)
        runner.add("media_scope.download_torrent", 0, download_payload(rank), rank=rank)
        runner.add("media_scope.movie_transfer", 0, transfer_payload(rank), rank=rank)
        runner.add("media_scope.movie_history", 0, history_payload(), rank=rank)
    cleared, clear_code = flow(tmp_path, runner).clear(
        blocked["run_id"], "Cleaned seedbox manually"
    )

    assert (clear_code, cleared["result"]) == (EXIT_OK, "flow_completed")
    assert [
        call["rank"] for call in runner.calls if call["module"] == "media_scope.movie_transfer"
    ] == [1, 2, 3]
    assert cleared["clear_reason"] == "Cleaned seedbox manually"


def test_unfinished_v1_manifest_remains_readable(tmp_path: Path) -> None:
    root = tmp_path / "flow"
    run_id = "movie-flow-20260809T160000Z-legacy01"
    run_directory = root / "runs" / run_id
    run_directory.mkdir(parents=True)
    stages = {
        key: {"position": position, "status": "PENDING", "attempts": []}
        for position, key in enumerate(
            ("recommendations", "search", "probe", "download", "transfer", "history"), start=1
        )
    }
    legacy = {
        "schema_version": 1,
        "result": "flow_running",
        "run_id": run_id,
        "state": "RUNNING",
        "current_stage": "recommendations",
        "created_at": "2026-08-09T16:00:00Z",
        "updated_at": "2026-08-09T16:00:00Z",
        "artifact_directory": str(run_directory),
        "stages": stages,
    }
    root.mkdir(exist_ok=True)
    (root / "latest-status.json").write_text(json.dumps(legacy), encoding="utf-8")
    (run_directory / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    payload, code = flow(tmp_path, BatchRunner()).status(run_id)

    assert (code, payload["schema_version"], payload["run_id"]) == (0, 1, run_id)


def test_unfinished_v1_manifest_continues_through_legacy_single_movie_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flow"
    run_id = "movie-flow-20260809T160000Z-legacy02"
    run_directory = root / "runs" / run_id
    run_directory.mkdir(parents=True)
    legacy = {
        "schema_version": 1,
        "result": "flow_running",
        "run_id": run_id,
        "state": "RUNNING",
        "current_stage": "recommendations",
        "created_at": "2026-08-09T16:00:00Z",
        "updated_at": "2026-08-09T16:00:00Z",
        "artifact_directory": str(run_directory),
        "stages": {
            key: {"position": position, "status": "PENDING", "attempts": []}
            for position, key in enumerate(
                ("recommendations", "search", "probe", "download", "transfer", "history"),
                start=1,
            )
        },
        "attention": None,
        "retryable_failure": None,
        "allowed_actions": ["status"],
    }
    root.mkdir(exist_ok=True)
    (root / "latest-status.json").write_text(json.dumps(legacy), encoding="utf-8")
    (run_directory / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    runner = BatchRunner()
    runner.add("media_scope.recommend_cli", 0, recommendation_payload(MOVIES[:1]))
    runner.add("media_scope.movie_search", 0, search_payload(MOVIES[:1]))
    runner.add("media_scope.movie_probe", 0, probe_payload(1))
    runner.add("media_scope.download_torrent", 0, download_payload(1))
    runner.add("media_scope.movie_transfer", 0, transfer_payload(1))
    runner.add("media_scope.movie_history", 0, history_payload())

    payload, code = flow(tmp_path, runner).run()

    assert (code, payload["schema_version"], payload["result"]) == (0, 1, "flow_completed")
    assert [call["module"] for call in runner.calls] == [
        "media_scope.recommend_cli",
        "media_scope.movie_search",
        "media_scope.movie_probe",
        "media_scope.download_torrent",
        "media_scope.movie_transfer",
        "media_scope.movie_history",
    ]
