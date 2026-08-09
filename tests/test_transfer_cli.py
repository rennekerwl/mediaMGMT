"""CLI and JSON contract tests for Step 7."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from media_scope.transfer_cli import main
from tests.fake_remote_filesystem import FakeRemoteFilesystem
from tests.test_transfer_input import transfer_result
from tests.test_transfer_service import FakeTransferClient


def client_factory(client: FakeTransferClient) -> Any:
    def factory(*_args: Any, **_kwargs: Any) -> FakeTransferClient:
        return client

    return factory


def filesystem_factory(filesystem: FakeRemoteFilesystem) -> Any:
    def factory(*_args: Any, **_kwargs: Any) -> FakeRemoteFilesystem:
        return filesystem

    return factory


def configure(monkeypatch: Any, movies: Path) -> None:
    monkeypatch.setenv("MOVIES_DIRECTORY", str(movies))
    monkeypatch.setenv("RTORRENT_DOWNLOAD_DIRECTORY", "/downloads")
    monkeypatch.setenv("RTORRENT_RPC_URL", "http://rtorrent.test/RPC")


def test_success_from_stdin_writes_matching_json_output(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    configure(monkeypatch, movies)
    handoff = transfer_result()
    source = "/downloads/1091-the-thing-aaaaaaaa/The Thing"
    remote = FakeRemoteFilesystem()
    remote.add_directory("/downloads/1091-the-thing-aaaaaaaa")
    remote.add_file(f"{source}/movie.mkv", size=6)
    client = FakeTransferClient(source)
    output = tmp_path / "transfer.json"

    code = main(
        ["--output", str(output), "--pretty", "--verbose"],
        input_text=json.dumps(handoff),
        client_factory=client_factory(client),
        filesystem_factory=filesystem_factory(remote),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["result"] == "transfer_completed"
    assert output.read_text(encoding="utf-8") == captured.out
    assert "Starting movie transfer job" in captured.err


def test_download_result_file_takes_precedence_over_stdin(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    configure(monkeypatch, movies)
    result_file = tmp_path / "download.json"
    result_file.write_text(json.dumps(transfer_result()), encoding="utf-8")
    source = "/downloads/1091-the-thing-aaaaaaaa/The Thing"
    remote = FakeRemoteFilesystem()
    remote.add_directory("/downloads/1091-the-thing-aaaaaaaa")
    remote.add_file(f"{source}/movie.mkv")

    code = main(
        ["--download-result", str(result_file)],
        input_text="invalid",
        client_factory=client_factory(FakeTransferClient(source)),
        filesystem_factory=filesystem_factory(remote),
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "TRANSFER_AND_CLEANUP_COMPLETED"


def test_invalid_input_and_missing_config_are_exit_two(capsys: Any, monkeypatch: Any) -> None:
    code = main([], input_text="")
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_DOWNLOAD_RESULT"

    monkeypatch.setenv("MOVIES_DIRECTORY", "")
    code = main([], input_text=json.dumps(transfer_result()))
    assert code == 2
    assert json.loads(capsys.readouterr().out)["result"] == "transfer_failed"


def test_cleanup_failure_is_explicit_partial_success(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    configure(monkeypatch, movies)
    source = "/downloads/1091-the-thing-aaaaaaaa/The Thing"
    remote = FakeRemoteFilesystem()
    remote.add_directory("/downloads/1091-the-thing-aaaaaaaa")
    remote.add_file(f"{source}/movie.mkv")
    client = FakeTransferClient(source, erase_fails=True)

    code = main(
        [],
        input_text=json.dumps(transfer_result()),
        client_factory=client_factory(client),
        filesystem_factory=filesystem_factory(remote),
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 7
    assert payload["result"] == "transfer_completed_cleanup_failed"
    assert payload["transfer"]["status"] == "COMPLETED"
    assert payload["cleanup"]["remaining_remote_paths"] == [source]


def test_password_is_never_serialized_or_logged(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    configure(monkeypatch, movies)
    monkeypatch.setenv("RTORRENT_RPC_PASSWORD", "never-print-this")
    monkeypatch.setenv("SEEDBOX_PASSWORD", "also-never-print-this")
    monkeypatch.setattr("media_scope.transfer_cli.load_dotenv", lambda: False)

    def failing_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("failed")

    code = main(
        [],
        input_text=json.dumps(transfer_result()),
        client_factory=failing_factory,
    )

    captured = capsys.readouterr()
    assert code == 9
    assert "never-print-this" not in captured.out + captured.err
    assert "also-never-print-this" not in captured.out + captured.err
