"""Exercise real child stages without invoking acquisition services."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from media_scope.movie_flow import run_process


@pytest.mark.parametrize("with_input", [False, True])
def test_stage_preserves_streams_exit_code_and_suppresses_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_input: bool
) -> None:
    actual_run = subprocess.run
    calls = []

    def checked_run(*args, **kwargs):
        calls.append(kwargs["creationflags"])
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", checked_run)
    input_path = tmp_path / "input.txt" if with_input else None
    if input_path is not None:
        input_path.write_text("stage input", encoding="utf-8")
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read() or 'empty'); "
        "sys.stderr.write('diagnostic'); sys.exit(4)",
    ]

    result = run_process(command, input_path, stdout_path, stderr_path, tmp_path)

    assert result == 4
    assert stdout_path.read_text(encoding="utf-8") == ("stage input" if with_input else "empty")
    assert stderr_path.read_text(encoding="utf-8") == "diagnostic"
    assert calls == [subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0]
