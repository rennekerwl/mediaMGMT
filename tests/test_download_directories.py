"""Tests for remote permanent and post-processing path safety."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from media_scope.download_directories import DownloadDirectoryManager, sanitize_component
from media_scope.exceptions import DownloadPostProcessingError, DownloadStorageError
from tests.fake_remote_filesystem import FakeRemoteFilesystem

HASH = "a" * 40


def manager(
    filesystem: FakeRemoteFilesystem, root: str = "/downloads", **kwargs: object
) -> DownloadDirectoryManager:
    return DownloadDirectoryManager(
        filesystem,
        root,
        tmdb_id=4608,
        title="30 Rock / ../../etc",
        infohash=HASH,
        **kwargs,
    )


def test_safe_deterministic_child_is_sanitized_and_created() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    path = value.prepare(existing_owner=False)
    assert path == PurePosixPath("/downloads/4608-30-rock-etc-aaaaaaaa")
    assert filesystem.lstat(path).is_directory


def test_existing_unowned_directory_is_a_collision() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    filesystem.add_directory(value.job_directory)
    with pytest.raises(DownloadStorageError) as captured:
        value.prepare(existing_owner=False)
    assert captured.value.error_code == "DOWNLOAD_PATH_COLLISION"


def test_owned_directory_is_restart_safe() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    filesystem.add_directory(value.job_directory)
    assert value.prepare(existing_owner=True) == value.job_directory


def test_dangerous_root_and_overlapping_probe_root_are_rejected() -> None:
    filesystem = FakeRemoteFilesystem()
    with pytest.raises(DownloadStorageError):
        manager(filesystem, "/")
    with pytest.raises(DownloadStorageError):
        manager(filesystem, "/downloads", probe_root="/downloads/probes")


def test_path_traversal_cannot_escape_root() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    assert ".." not in value.job_directory.name
    assert sanitize_component("../../") == "untitled"
    with pytest.raises(DownloadStorageError):
        manager(filesystem, "/downloads/../outside")


def test_final_path_must_exist_under_download_or_allowed_root() -> None:
    filesystem = FakeRemoteFilesystem()
    filesystem.add_directory("/library/Example")
    value = manager(filesystem, allowed_final_roots=["/library"])
    assert value.validate_final_path("/library/Example") == PurePosixPath("/library/Example")
    filesystem.add_directory("/outside")
    with pytest.raises(DownloadPostProcessingError) as captured:
        value.validate_final_path("/outside")
    assert captured.value.error_code == "FINAL_PATH_OUTSIDE_ALLOWED_ROOT"


def test_missing_final_path_is_rejected() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    with pytest.raises(DownloadPostProcessingError) as captured:
        value.validate_final_path(str(value.job_directory / "missing"))
    assert captured.value.error_code == "FINAL_PATH_NOT_FOUND"


def test_symlink_is_not_counted_or_returned() -> None:
    filesystem = FakeRemoteFilesystem()
    value = manager(filesystem)
    filesystem.add_directory(value.job_directory)
    filesystem.add_file(value.job_directory / "episode.mkv", size=10)
    filesystem.add_symlink(value.job_directory / "escape")
    assert value.on_disk_size(value.job_directory) == 10
    assert value.top_level_paths(value.job_directory, value.job_directory) == [
        str(value.job_directory / "episode.mkv")
    ]
