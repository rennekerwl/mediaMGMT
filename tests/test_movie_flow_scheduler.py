"""Read-only validation of the Windows Task Scheduler installer."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler is Windows-only")
def test_scheduler_installer_dry_run_has_safe_defaults() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "install_movie_flow_task.ps1"

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-DryRun",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["operation"] == "install"
    assert payload["task_name"] == "mediaMGMT Movie Flow"
    assert payload["interval_minutes"] == 15
    assert payload["execute"].endswith(".venv\\Scripts\\pythonw.exe")
    assert payload["arguments"] == "-m media_scope.windowless_launcher"
    assert payload["logon_type"] == "Interactive"
    assert payload["triggers"] == ["AtLogOn", "DailyRepeating"]
    assert payload["repetition_duration_hours"] == 24
    assert payload["run_while_logged_out"] is False
    assert payload["dry_run"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler is Windows-only")
def test_scheduler_remove_dry_run_does_not_unregister_anything() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "install_movie_flow_task.ps1"

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Remove",
            "-DryRun",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "operation": "remove",
        "task_name": "mediaMGMT Movie Flow",
        "media_and_history_preserved": True,
        "dry_run": True,
    }
