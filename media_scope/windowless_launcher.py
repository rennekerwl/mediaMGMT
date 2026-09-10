"""Run the scheduled movie flow without creating a visible console window.

The scheduled task starts this module with ``pythonw.exe``.  The launcher then
starts the normal console Python executable as a child so the flow keeps its
usual interpreter and command line while inheriting no console window.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

EXIT_LAUNCHER_FAILURE = 5
LOG_RETENTION = 30
CHILD_ARGUMENTS = ("-m", "media_scope.movie_flow", "run")


def _repository_root() -> Path:
    """Return the repository root containing the ``media_scope`` package."""

    return Path(__file__).resolve().parents[1]


def _sibling_python_executable() -> Path:
    """Return ``python.exe`` beside the interpreter running this module."""

    return Path(sys.executable).with_name("python.exe")


def _log_directory(repository_root: Path) -> Path:
    return repository_root / ".logs" / "movie-flow"


def _new_log_path(log_directory: Path, *, retention: int = LOG_RETENTION) -> Path:
    """Create a unique per-run log path and prune older launcher logs."""

    if retention < 1:
        raise ValueError("log retention must be at least one file")
    log_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = log_directory / f"run-{timestamp}-{os.getpid()}.log"
    path.touch(exist_ok=False)

    logs = sorted(log_directory.glob("run-*.log"), key=lambda item: item.name)
    for old_log in logs[:-retention]:
        try:
            old_log.unlink()
        except OSError:
            # A locked old log must not prevent the scheduled flow from running.
            pass
    return path


def _creation_flags() -> int:
    """Return the Windows flag that prevents child console creation."""

    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def _write_failure(log_path: Path | None, error: BaseException) -> None:
    """Best-effort persistence for launcher errors."""

    if log_path is None:
        return
    try:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("\nLauncher failure:\n")
            traceback.print_exception(error, file=stream)
    except OSError:
        pass


def run_launcher(
    command: Sequence[str] | None = None,
    *,
    repository_root: Path | None = None,
    log_directory: Path | None = None,
    python_executable: Path | None = None,
) -> int:
    """Run the movie flow and return its exit code.

    ``command`` is an internal callable injection point used by harmless
    integration tests.  The production entry point leaves it unset, so the
    only production child command is the fixed movie-flow invocation.
    """

    root = (repository_root or _repository_root()).resolve()
    log_path: Path | None = None
    try:
        if not root.is_dir():
            raise FileNotFoundError(f"repository root does not exist: {root}")
        if command is None:
            executable = python_executable or _sibling_python_executable()
            child_command = [str(executable), *CHILD_ARGUMENTS]
        else:
            child_command = [str(part) for part in command]
            if not child_command:
                raise ValueError("launcher command cannot be empty")

        log_path = _new_log_path(log_directory or _log_directory(root))
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"Started {datetime.now(UTC).isoformat()}\n"
                f"Working directory: {root}\n"
                f"Command: {child_command!r}\n"
                "Child stdout and stderr follow.\n\n"
            )
            log_file.flush()
            popen_kwargs: dict[str, object] = {
                "cwd": root,
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
            }
            flags = _creation_flags()
            if flags:
                popen_kwargs["creationflags"] = flags
            process = subprocess.Popen(child_command, **popen_kwargs)
            exit_code = int(process.wait())
            log_file.write(f"\nFinished with exit code {exit_code}\n")
            return exit_code
    except Exception as error:
        _write_failure(log_path, error)
        return EXIT_LAUNCHER_FAILURE


def main() -> int:
    """Run the fixed scheduled movie-flow command."""

    return run_launcher()


if __name__ == "__main__":
    raise SystemExit(main())
