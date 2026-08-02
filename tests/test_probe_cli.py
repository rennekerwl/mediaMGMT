"""JSON/exit-code tests for the separately executable probe CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from media_scope.probe_cli import main
from tests.test_probe_input import HASH_A, HASH_B, candidate, report
from tests.test_probe_service import FakeRtorrent, metadata


def write_report(path: Path, *values: dict[str, Any]) -> Path:
    path.write_text(json.dumps(report(*values)), encoding="utf-8")
    return path


def test_missing_search_results_argument_is_json_exit_two(capsys: Any) -> None:
    code = main([])
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["error_code"] == "INVALID_CLI_INPUT"
    assert captured.err == ""


def test_dry_run_sorts_limits_and_never_calls_rtorrent(
    tmp_path: Path,
    capsys: Any,
) -> None:
    path = write_report(tmp_path / "search.json", candidate(2, HASH_B), candidate(1))

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry run called rTorrent")

    code = main(
        ["--search-results", str(path), "--dry-run", "--max-candidates", "1"],
        client_factory=forbidden,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"] == "dry_run"
    assert payload["rtorrent_called"] is False
    assert [item["original_rank"] for item in payload["planned_probe_order"]] == [1]
    assert payload["unattempted_candidates"][0]["original_rank"] == 2


def test_dry_run_output_file_matches_stdout(tmp_path: Path, capsys: Any) -> None:
    path = write_report(tmp_path / "search.json", candidate(1))
    output = tmp_path / "result.json"
    code = main(
        [
            "--search-results",
            str(path),
            "--dry-run",
            "--pretty",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert output.read_text(encoding="utf-8") == captured.out
    json.loads(captured.out)


def test_empty_candidate_input_is_exit_three(tmp_path: Path, capsys: Any) -> None:
    path = write_report(tmp_path / "search.json")
    code = main(["--search-results", str(path), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["error_code"] == "NO_CANDIDATES"


def test_missing_rtorrent_configuration_is_exit_four(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    path = write_report(tmp_path / "search.json", candidate(1))
    monkeypatch.setenv("RTORRENT_RPC_URL", "")
    code = main(["--search-results", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["error_code"] == "RTORRENT_CONFIGURATION_MISSING"


class CliFakeRtorrent(FakeRtorrent):
    def __enter__(self) -> CliFakeRtorrent:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def test_mocked_probe_rank_one_fails_and_rank_two_succeeds(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    path = write_report(tmp_path / "search.json", candidate(1), candidate(2, HASH_B))
    monkeypatch.setenv("RTORRENT_RPC_URL", "http://rtorrent.test/RPC")
    monkeypatch.setenv("RTORRENT_PROBE_DIRECTORY", "/remote/home/probes")
    client = CliFakeRtorrent({HASH_B: [metadata(True)]})
    client.fail_submission.add(HASH_A.upper())

    def factory(*_args: Any, **_kwargs: Any) -> CliFakeRtorrent:
        return client

    code = main(
        ["--search-results", str(path), "--skip-preflight", "--pretty", "--verbose"],
        client_factory=factory,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert [item["status"] for item in payload["attempts"]] == [
        "SUBMISSION_FAILED",
        "METADATA_RETRIEVED",
    ]
    assert payload["selected_candidate"]["original_rank"] == 2
    assert "Probing rank 1" in captured.err
    assert captured.out.lstrip().startswith("{")


def test_connection_diagnostic_adds_no_torrent(capsys: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("RTORRENT_RPC_URL", "http://rtorrent.test/RPC")
    monkeypatch.setenv("RTORRENT_PROBE_DIRECTORY", "/remote/home/probes")
    client = CliFakeRtorrent({})

    def factory(*_args: Any, **_kwargs: Any) -> CliFakeRtorrent:
        return client

    code = main(["check-connection"], client_factory=factory)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"] == "connection_ok"
    assert payload["probe_directory_ready"] is True
    assert payload["torrent_added"] is False
    assert not any(call[0] == "submit" for call in client.calls)


def test_stdout_is_valid_json_and_password_is_absent_on_failure(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    path = write_report(tmp_path / "search.json", candidate(1))
    monkeypatch.setenv("RTORRENT_RPC_PASSWORD", "never-print-this")
    monkeypatch.delenv("RTORRENT_RPC_URL", raising=False)
    code = main(["--search-results", str(path), "--verbose"])
    captured = capsys.readouterr()
    assert code == 4
    json.loads(captured.out)
    assert "never-print-this" not in captured.out
    assert "never-print-this" not in captured.err
