"""Tests for the scheduled task's windowless process boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from media_scope import windowless_launcher


class FakeProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def test_launcher_redirects_child_io_and_propagates_exit_code(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        kwargs["stdout"].write("child stdout\nchild stderr\n")
        kwargs["stdout"].flush()
        return FakeProcess(4)

    monkeypatch.setattr(windowless_launcher.subprocess, "Popen", fake_popen)

    result = windowless_launcher.run_launcher(
        command=["test-child"],
        repository_root=tmp_path,
        log_directory=tmp_path / "logs",
    )

    assert result == 4
    assert captured["command"] == ["test-child"]
    assert captured["cwd"] == tmp_path
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.STDOUT
    if os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in captured
    logs = list((tmp_path / "logs").glob("run-*.log"))
    assert len(logs) == 1
    assert "child stdout" in logs[0].read_text(encoding="utf-8")


def test_launcher_uses_sibling_console_python_for_production_command(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        return FakeProcess(0)

    monkeypatch.setattr(windowless_launcher.subprocess, "Popen", fake_popen)
    executable = tmp_path / ".venv" / "Scripts" / "python.exe"

    assert (
        windowless_launcher.run_launcher(
            repository_root=tmp_path,
            log_directory=tmp_path / "logs",
            python_executable=executable,
        )
        == 0
    )
    assert captured["command"] == [
        str(executable),
        "-m",
        "media_scope.movie_flow",
        "run",
    ]


def test_launcher_returns_five_and_persists_launch_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        raise OSError("child executable was not found")

    monkeypatch.setattr(windowless_launcher.subprocess, "Popen", fake_popen)

    result = windowless_launcher.run_launcher(
        command=["missing-child"],
        repository_root=tmp_path,
        log_directory=tmp_path / "logs",
    )

    assert result == windowless_launcher.EXIT_LAUNCHER_FAILURE
    logs = list((tmp_path / "logs").glob("run-*.log"))
    assert len(logs) == 1
    assert "child executable was not found" in logs[0].read_text(encoding="utf-8")


def test_launcher_log_retention_is_bounded(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    for index in range(31):
        (log_directory / f"run-20200101T000000.{index:06d}Z-1.log").write_text(
            "old\n", encoding="utf-8"
        )

    newest = windowless_launcher._new_log_path(log_directory, retention=30)

    assert newest.is_file()
    assert len(list(log_directory.glob("run-*.log"))) == 30


@pytest.mark.skipif(os.name != "nt", reason="pythonw is Windows-only")
def test_real_pythonw_runs_injected_harmless_child_without_console_output(
    tmp_path: Path,
) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("the active Python installation has no pythonw.exe")
    log_directory = tmp_path / "logs"
    harness = """
from pathlib import Path
import sys
import ctypes
from media_scope.windowless_launcher import run_launcher

assert ctypes.windll.kernel32.GetConsoleWindow() == 0, 'launcher has a console'
child = [
    str(Path(sys.executable).with_name("python.exe")),
    "-c",
    "import sys, ctypes; assert ctypes.windll.kernel32.GetConsoleWindow() == 0; "
    "print('child stdout'); print('child stderr', file=sys.stderr); sys.exit(7)",
]
raise SystemExit(
    run_launcher(
        command=child,
        repository_root=Path.cwd(),
        log_directory=Path(sys.argv[1]),
    )
)
"""

    completed = subprocess.run(
        [str(pythonw), "-c", harness, str(log_directory)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 7, completed.stderr.decode(errors="replace")
    assert completed.stdout == b""
    assert completed.stderr == b""
    logs = list(log_directory.glob("run-*.log"))
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert "child stdout" in content
    assert "child stderr" in content
