"""Tests for permanent and post-processing path safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_scope.download_directories import DownloadDirectoryManager, sanitize_component
from media_scope.exceptions import DownloadPostProcessingError, DownloadStorageError

HASH = "a" * 40


def manager(root: Path, **kwargs: object) -> DownloadDirectoryManager:
    return DownloadDirectoryManager(
        root.resolve(),
        tmdb_id=4608,
        title="30 Rock / ../../etc",
        infohash=HASH,
        **kwargs,
    )


def test_safe_deterministic_child_is_sanitized_and_created(tmp_path: Path) -> None:
    value = manager(tmp_path / "downloads")
    path = value.prepare(existing_owner=False)
    assert path.parent == (tmp_path / "downloads").resolve()
    assert path.name == "4608-30-rock-etc-aaaaaaaa"
    assert path.is_dir()


def test_existing_unowned_directory_is_a_collision(tmp_path: Path) -> None:
    value = manager(tmp_path / "downloads")
    value.job_directory.mkdir(parents=True)
    with pytest.raises(DownloadStorageError) as captured:
        value.prepare(existing_owner=False)
    assert captured.value.error_code == "DOWNLOAD_PATH_COLLISION"


def test_owned_directory_is_restart_safe(tmp_path: Path) -> None:
    value = manager(tmp_path / "downloads")
    value.job_directory.mkdir(parents=True)
    assert value.prepare(existing_owner=True) == value.job_directory


def test_dangerous_root_and_overlapping_probe_root_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DownloadStorageError):
        manager(Path(Path.home().anchor))
    root = tmp_path / "downloads"
    with pytest.raises(DownloadStorageError):
        manager(root, probe_root=root / "probes")


def test_path_traversal_cannot_escape_root(tmp_path: Path) -> None:
    value = manager(tmp_path / "downloads")
    assert ".." not in value.job_directory.name
    assert sanitize_component("../../") == "untitled"


def test_final_path_must_exist_under_download_or_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    allowed = tmp_path / "library"
    value = manager(root, allowed_final_roots=[allowed])
    moved = allowed / "Example"
    moved.mkdir(parents=True)
    assert value.validate_final_path(str(moved)) == moved.resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(DownloadPostProcessingError) as captured:
        value.validate_final_path(str(outside))
    assert captured.value.error_code == "FINAL_PATH_OUTSIDE_ALLOWED_ROOT"


def test_missing_final_path_is_rejected(tmp_path: Path) -> None:
    value = manager(tmp_path / "downloads")
    with pytest.raises(DownloadPostProcessingError) as captured:
        value.validate_final_path(str(value.job_directory / "missing"))
    assert captured.value.error_code == "FINAL_PATH_NOT_FOUND"
