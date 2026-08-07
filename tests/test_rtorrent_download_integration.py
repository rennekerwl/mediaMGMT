"""Opt-in legal live integration test for the retained Step 5 torrent."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from media_scope.download_cli import main


@pytest.mark.skipif(
    os.getenv("RUN_RTORRENT_DOWNLOAD_INTEGRATION_TESTS", "").casefold() != "true",
    reason="Live full-download integration tests are explicitly disabled.",
)
def test_user_supplied_legal_small_torrent_download(monkeypatch: pytest.MonkeyPatch) -> None:
    health_text = os.getenv("RTORRENT_DOWNLOAD_INTEGRATION_HEALTH_RESULT", "").strip()
    directory_text = os.getenv("RTORRENT_DOWNLOAD_INTEGRATION_DIRECTORY", "").strip()
    if not health_text or not directory_text:
        pytest.fail(
            "Set RTORRENT_DOWNLOAD_INTEGRATION_HEALTH_RESULT and the dedicated "
            "RTORRENT_DOWNLOAD_INTEGRATION_DIRECTORY as an absolute seedbox POSIX path."
        )
    health = Path(health_text).resolve()
    directory = PurePosixPath(directory_text)
    if not health.is_file() or not directory.is_absolute() or ".." in directory.parts:
        pytest.fail("The integration health result must exist and the directory must be absolute.")
    print(
        "SIDE EFFECTS: resumes the existing legal Step 5 torrent, writes payload under "
        f"{directory}, retains the torrent, and does not erase or automatically clean up files."
    )
    monkeypatch.setenv("RTORRENT_DOWNLOAD_DIRECTORY", str(directory))
    code = main(
        [
            "--health-result",
            str(health),
            "--post-completion-policy",
            "stop",
            "--pretty",
        ]
    )
    assert code == 0
