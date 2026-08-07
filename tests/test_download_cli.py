"""JSON, exit-code, and mutation tests for the Step 6 CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from media_scope.download_cli import main
from tests.fake_remote_filesystem import FakeRemoteFilesystem
from tests.test_download_input import health_result
from tests.test_download_service import FakeDownloadClient, snapshot


class CliFakeDownloadClient(FakeDownloadClient):
    def __init__(
        self,
        rpc_url: str,
        *,
        snapshots: list[Any],
        custom: dict[str, str] | None = None,
        exists: list[bool] | None = None,
        **_kwargs: Any,
    ) -> None:
        super().__init__(snapshots, custom=custom, exists=exists)
        self.sanitized_endpoint = rpc_url

    def __enter__(self) -> CliFakeDownloadClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def write_health(path: Path) -> Path:
    path.write_text(json.dumps(health_result()), encoding="utf-8")
    return path


def configure(monkeypatch: Any, root: Path) -> None:
    monkeypatch.setenv("RTORRENT_RPC_URL", "http://rtorrent.test/RPC")
    monkeypatch.setenv("RTORRENT_DOWNLOAD_DIRECTORY", "/downloads")
    monkeypatch.setenv("RTORRENT_POST_PROCESS_GRACE_SECONDS", "0")


def factory_for(client: CliFakeDownloadClient) -> Any:
    def factory(*_args: Any, **_kwargs: Any) -> CliFakeDownloadClient:
        return client

    return factory


def filesystem_factory(filesystem: FakeRemoteFilesystem) -> Any:
    def factory(*_args: Any, **_kwargs: Any) -> FakeRemoteFilesystem:
        return filesystem

    return factory


def test_missing_health_result_is_json_exit_two(capsys: Any) -> None:
    code = main([])
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["error_code"] == "INVALID_CLI_INPUT"
    assert captured.err == ""


def test_malformed_health_result_is_exit_two(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "health.json"
    path.write_text("{", encoding="utf-8")
    code = main(["--health-result", str(path)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_HEALTH_RESULT"


def test_dry_run_contacts_rtorrent_but_never_mutates(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    root = tmp_path / "downloads"
    configure(monkeypatch, root)
    target = (root / "4608-30-rock-aaaaaaaa").resolve()
    client = CliFakeDownloadClient(
        "http://rtorrent.test/RPC",
        snapshots=[snapshot(target, active=False, rate=0)],
    )
    code = main(
        ["--health-result", str(write_health(tmp_path / "health.json")), "--dry-run"],
        client_factory=factory_for(client),
        filesystem_factory=filesystem_factory(FakeRemoteFilesystem()),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"] == "dry_run"
    assert any(call[0] == "snapshot" for call in client.calls)
    assert not root.exists()
    assert not any(
        call[0] in {"start", "stop", "directory", "tag", "custom_state"} for call in client.calls
    )


def test_success_stdout_matches_output_and_logs_only_to_stderr(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    root = tmp_path / "downloads"
    configure(monkeypatch, root)
    target = (root / "4608-30-rock-aaaaaaaa").resolve()
    client = CliFakeDownloadClient(
        "http://rtorrent.test/RPC",
        snapshots=[snapshot(target, completed=100, complete=True, rate=0)],
    )
    output = tmp_path / "download.json"
    filesystem = FakeRemoteFilesystem()
    filesystem.add_file("/downloads/4608-30-rock-aaaaaaaa/payload/episode.mkv", size=4)
    code = main(
        [
            "--health-result",
            str(write_health(tmp_path / "health.json")),
            "--output",
            str(output),
            "--pretty",
            "--verbose",
        ],
        client_factory=factory_for(client),
        filesystem_factory=filesystem_factory(filesystem),
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert code == 0
    assert parsed["status"] == "READY_FOR_TRANSFER"
    assert output.read_text(encoding="utf-8") == captured.out
    assert "Starting download job" in captured.err
    assert captured.out.lstrip().startswith("{")


def test_missing_torrent_uses_exit_three(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    root = tmp_path / "downloads"
    configure(monkeypatch, root)
    target = (root / "4608-30-rock-aaaaaaaa").resolve()
    client = CliFakeDownloadClient(
        "http://rtorrent.test/RPC", snapshots=[snapshot(target)], exists=[False]
    )
    code = main(
        ["--health-result", str(write_health(tmp_path / "health.json"))],
        client_factory=factory_for(client),
        filesystem_factory=filesystem_factory(FakeRemoteFilesystem()),
    )
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["error_code"]) == (3, "SELECTED_TORRENT_NOT_FOUND")


def test_password_is_never_serialized_or_logged(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("RTORRENT_RPC_PASSWORD", "never-print-this")
    monkeypatch.delenv("RTORRENT_RPC_URL", raising=False)
    monkeypatch.setenv("RTORRENT_DOWNLOAD_DIRECTORY", str((tmp_path / "downloads").resolve()))
    code = main(["--health-result", str(write_health(tmp_path / "health.json")), "--verbose"])
    captured = capsys.readouterr()
    assert code == 4
    json.loads(captured.out)
    assert "never-print-this" not in captured.out
    assert "never-print-this" not in captured.err


def test_stalled_download_is_valid_json_exit_six(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    root = tmp_path / "downloads"
    configure(monkeypatch, root)
    target = (root / "4608-30-rock-aaaaaaaa").resolve()
    client = CliFakeDownloadClient(
        "http://rtorrent.test/RPC",
        snapshots=[
            snapshot(target, active=False, rate=0, peers=0),
            snapshot(target, rate=0, peers=0),
            snapshot(target, rate=0, peers=0),
        ],
    )
    code = main(
        [
            "--health-result",
            str(write_health(tmp_path / "health.json")),
            "--poll-interval-seconds",
            "1",
            "--stall-timeout-seconds",
            "1",
        ],
        client_factory=factory_for(client),
        filesystem_factory=filesystem_factory(FakeRemoteFilesystem()),
    )
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["error_code"]) == (6, "DOWNLOAD_STALLED")
    assert payload["ready_for_transfer"] is False


def test_legacy_disk_reserve_is_ignored(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    root = tmp_path / "downloads"
    configure(monkeypatch, root)
    monkeypatch.setenv("RTORRENT_MIN_FREE_SPACE_BYTES", "999999999999")
    target = (root / "4608-30-rock-aaaaaaaa").resolve()
    client = CliFakeDownloadClient(
        "http://rtorrent.test/RPC",
        snapshots=[snapshot(target, completed=100, complete=True, rate=0)],
    )
    filesystem = FakeRemoteFilesystem()
    filesystem.add_file("/downloads/4608-30-rock-aaaaaaaa/payload/episode.mkv")
    code = main(
        ["--health-result", str(write_health(tmp_path / "health.json"))],
        client_factory=factory_for(client),
        filesystem_factory=filesystem_factory(filesystem),
    )
    assert code == 0
    assert "filesystem_free_bytes" not in capsys.readouterr().out
