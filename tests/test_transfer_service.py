"""Transfer, safety, and cleanup ordering tests for Step 7."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from media_scope.download_models import DownloadCapabilities, DownloadSnapshot
from media_scope.exceptions import (
    RtorrentRpcError,
    SeedboxFilesystemError,
    TransferCleanupError,
    TransferStorageError,
)
from media_scope.transfer_input import MovieTransferInput, parse_transfer_input
from media_scope.transfer_service import MovieTransferService
from tests.fake_remote_filesystem import FakeRemoteFilesystem
from tests.test_transfer_input import HASH, transfer_result


class FakeTransferClient:
    sanitized_endpoint = "http://rtorrent.test/RPC"

    def __init__(
        self,
        base_path: str,
        *,
        erase_fails: bool = False,
        custom: dict[str, str] | None = None,
        complete: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.base_path = base_path
        self.erase_fails = erase_fails
        self.present = True
        self.calls: list[tuple[Any, ...]] = []
        self.custom = custom or {
            "media_download_job_id": "download-1091-aaaaaaaaaaaa",
            "media_download_state": "READY_FOR_TRANSFER",
        }
        self.complete = complete
        self.events = events

    def __enter__(self) -> FakeTransferClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def discover_download_capabilities(self) -> DownloadCapabilities:
        self.calls.append(("capabilities",))
        return DownloadCapabilities(
            "0.9.8", "0.13.8", "9", frozenset({"d.erase"}), "d.is_meta", "d.hash"
        )

    def torrent_exists(self, infohash: str) -> bool:
        self.calls.append(("exists", infohash))
        return self.present

    def get_custom(self, infohash: str, name: str) -> str | None:
        self.calls.append(("custom", infohash, name))
        return self.custom.get(name)

    def download_snapshot(self, infohash: str) -> DownloadSnapshot:
        self.calls.append(("snapshot", infohash))
        return DownloadSnapshot(
            infohash=HASH,
            name="The Thing",
            metadata_retrieved=True,
            state=0,
            is_active=False,
            is_open=False,
            complete=self.complete,
            completed_bytes=100 if self.complete else 50,
            size_bytes=100,
            left_bytes=0 if self.complete else 50,
            download_rate=0,
            upload_rate=0,
            uploaded_bytes=0,
            connected_peers=0,
            complete_peers=0,
            message=None,
            base_path=self.base_path,
            directory="/downloads/1091-the-thing-aaaaaaaa",
            ratio=0,
            hashing=False,
        )

    def stop(self, infohash: str) -> None:
        self.calls.append(("stop", infohash))
        if self.events is not None:
            self.events.append("stop")

    def close_download(self, infohash: str) -> None:
        self.calls.append(("close", infohash))
        if self.events is not None:
            self.events.append("close")

    def erase(self, infohash: str) -> None:
        self.calls.append(("erase", infohash))
        if self.events is not None:
            self.events.append("erase")
        if self.erase_fails:
            raise RtorrentRpcError("erase failed")
        self.present = False


class InterruptedFilesystem(FakeRemoteFilesystem):
    def __init__(self) -> None:
        super().__init__()
        self.download_count = 0

    def download_file(self, source: PurePosixPath, destination: Path) -> None:
        self.download_count += 1
        if self.download_count == 2:
            raise OSError("connection lost")
        super().download_file(source, destination)


class DisconnectedFilesystem(FakeRemoteFilesystem):
    def download_file(self, source: PurePosixPath, destination: Path) -> None:
        raise SeedboxFilesystemError("SFTP unavailable")


class EventFilesystem(FakeRemoteFilesystem):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def download_file(self, source: PurePosixPath, destination: Path) -> None:
        super().download_file(source, destination)
        self.events.append("download")

    def remove_tree(self, path: PurePosixPath) -> None:
        self.events.append("remove")
        super().remove_tree(path)


class RemovalFailureFilesystem(FakeRemoteFilesystem):
    def __init__(self, fail_path: str) -> None:
        super().__init__()
        self.fail_path = PurePosixPath(fail_path)

    def remove_tree(self, path: PurePosixPath) -> None:
        if path == self.fail_path:
            raise OSError("remove failed")
        super().remove_tree(path)


def prepare(
    tmp_path: Path,
    *,
    filesystem: FakeRemoteFilesystem | None = None,
    payload: dict[str, object] | None = None,
    allowed: tuple[str, ...] = (),
    client: FakeTransferClient | None = None,
) -> tuple[
    MovieTransferService,
    MovieTransferInput,
    FakeRemoteFilesystem,
    FakeTransferClient,
    Path,
]:
    value = payload or transfer_result()
    handoff = parse_transfer_input(value)
    remote = filesystem or FakeRemoteFilesystem()
    remote.add_directory(handoff.download_directory)
    movies = tmp_path / "movies"
    movies.mkdir(parents=True)
    rpc = client or FakeTransferClient(str(handoff.final_base_path))
    service = MovieTransferService(
        rpc,  # type: ignore[arg-type]
        remote,
        movies,
        download_root="/downloads",
        allowed_final_roots=allowed,
    )
    return service, handoff, remote, rpc, movies


def test_recursive_transfer_then_ordered_cleanup(tmp_path: Path) -> None:
    events: list[str] = []
    remote = EventFilesystem(events)
    client = FakeTransferClient(
        "/downloads/1091-the-thing-aaaaaaaa/The Thing",
        events=events,
    )
    service, handoff, remote, client, movies = prepare(
        tmp_path,
        filesystem=remote,
        client=client,
    )
    remote.add_file(handoff.final_base_path / "movie.mkv", size=8)
    remote.add_file(handoff.final_base_path / "Subs" / "movie.srt", size=3)

    payload = service.run(handoff)

    assert payload["status"] == "TRANSFER_AND_CLEANUP_COMPLETED"
    assert payload["transfer"]["file_count"] == 2
    assert payload["transfer"]["bytes_transferred"] == 11
    assert (movies / "The Thing" / "movie.mkv").read_bytes() == b"x" * 8
    assert not remote.exists(handoff.final_base_path)
    assert not remote.exists(handoff.download_directory)
    mutation_order = [call[0] for call in client.calls if call[0] in {"stop", "close", "erase"}]
    assert mutation_order == ["stop", "close", "erase"]
    assert events[:5] == ["download", "download", "stop", "close", "erase"]
    assert events[5] == "remove"


def test_single_file_and_multiple_top_level_sources(tmp_path: Path) -> None:
    value = transfer_result()
    job = "/downloads/1091-the-thing-aaaaaaaa"
    value["paths"] = {
        "final_base_path": job,
        "top_level_paths": [f"{job}/movie.mkv", f"{job}/Subs"],
    }
    service, handoff, remote, _client, movies = prepare(tmp_path, payload=value)
    remote.add_file(f"{job}/movie.mkv", size=5)
    remote.add_file(f"{job}/Subs/movie.srt", size=2)

    payload = service.run(handoff)

    assert payload["transfer"]["file_count"] == 2
    assert (movies / "movie.mkv").is_file()
    assert (movies / "Subs" / "movie.srt").is_file()


def test_filebot_allowed_path_is_transferred_and_removed(tmp_path: Path) -> None:
    value = transfer_result()
    value["paths"] = {
        "final_base_path": "/library/The Thing",
        "top_level_paths": ["/library/The Thing"],
    }
    client = FakeTransferClient("/library/The Thing")
    service, handoff, remote, _client, movies = prepare(
        tmp_path,
        payload=value,
        allowed=("/library",),
        client=client,
    )
    remote.add_file("/library/The Thing/movie.mkv", size=4)

    service.run(handoff)

    assert (movies / "The Thing" / "movie.mkv").is_file()
    assert not remote.exists(PurePosixPath("/library/The Thing"))


def test_filebot_move_may_leave_no_download_job_directory(tmp_path: Path) -> None:
    value = transfer_result()
    value["paths"] = {
        "final_base_path": "/library/The Thing",
        "top_level_paths": ["/library/The Thing"],
    }
    client = FakeTransferClient("/library/The Thing")
    service, handoff, remote, _client, movies = prepare(
        tmp_path,
        payload=value,
        allowed=("/library",),
        client=client,
    )
    remote.remove_tree(handoff.download_directory)
    remote.add_file("/library/The Thing/movie.mkv", size=4)

    payload = service.run(handoff)

    assert (movies / "The Thing" / "movie.mkv").is_file()
    assert payload["cleanup"]["download_directory_removed"] is False


def test_collision_prevents_transfer_and_cleanup(tmp_path: Path) -> None:
    service, handoff, remote, client, movies = prepare(tmp_path)
    remote.add_file(handoff.final_base_path / "movie.mkv")
    (movies / "The Thing").mkdir()

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.error_code == "DESTINATION_COLLISION"
    assert remote.exists(handoff.final_base_path)
    assert not any(call[0] in {"stop", "close", "erase"} for call in client.calls)


def test_nested_symlink_prevents_transfer_and_cleanup(tmp_path: Path) -> None:
    service, handoff, remote, client, _movies = prepare(tmp_path)
    remote.add_file(handoff.final_base_path / "movie.mkv")
    remote.add_symlink(handoff.final_base_path / "escape")

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.error_code == "UNSAFE_REMOTE_PATH"
    assert not any(call[0] == "download_file" for call in remote.calls)
    assert not any(call[0] == "erase" for call in client.calls)


def test_case_colliding_nested_names_are_rejected_before_transfer(tmp_path: Path) -> None:
    service, handoff, remote, client, _movies = prepare(tmp_path)
    remote.add_file(handoff.final_base_path / "Movie.mkv")
    remote.add_file(handoff.final_base_path / "movie.mkv")

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.error_code == "DESTINATION_COLLISION"
    assert not any(call[0] == "download_file" for call in remote.calls)
    assert not any(call[0] == "erase" for call in client.calls)


def test_seedbox_home_cannot_be_configured_as_a_cleanup_root(tmp_path: Path) -> None:
    service, handoff, remote, client, _movies = prepare(tmp_path)
    service = MovieTransferService(
        client,  # type: ignore[arg-type]
        remote,
        tmp_path / "movies",
        download_root="/home/seedboxer1",
        allowed_final_roots=("/downloads",),
    )
    remote.add_file(handoff.final_base_path / "movie.mkv")

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.error_code == "UNSAFE_REMOTE_ROOT"


def test_interrupted_transfer_preserves_torrent_and_remote_payload(tmp_path: Path) -> None:
    remote = InterruptedFilesystem()
    service, handoff, remote, client, movies = prepare(tmp_path, filesystem=remote)
    remote.add_file(handoff.final_base_path / "a.mkv", size=3)
    remote.add_file(handoff.final_base_path / "b.srt", size=2)

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.error_code == "SFTP_TRANSFER_FAILED"
    assert (movies / "The Thing" / "a.mkv").is_file()
    assert remote.exists(handoff.final_base_path / "a.mkv")
    assert remote.exists(handoff.final_base_path / "b.srt")
    assert not any(call[0] in {"stop", "close", "erase"} for call in client.calls)


def test_sftp_disconnect_uses_connection_exit_and_preserves_seedbox(tmp_path: Path) -> None:
    remote = DisconnectedFilesystem()
    service, handoff, remote, client, _movies = prepare(tmp_path, filesystem=remote)
    remote.add_file(handoff.final_base_path / "movie.mkv")

    with pytest.raises(TransferStorageError) as captured:
        service.run(handoff)

    assert captured.value.exit_code == 4
    assert remote.exists(handoff.final_base_path / "movie.mkv")
    assert not any(call[0] in {"stop", "close", "erase"} for call in client.calls)


def test_erase_failure_leaves_all_remote_files(tmp_path: Path) -> None:
    client = FakeTransferClient("/downloads/1091-the-thing-aaaaaaaa/The Thing", erase_fails=True)
    service, handoff, remote, _client, movies = prepare(tmp_path, client=client)
    remote.add_file(handoff.final_base_path / "movie.mkv", size=4)

    with pytest.raises(TransferCleanupError) as captured:
        service.run(handoff)

    assert (movies / "The Thing" / "movie.mkv").is_file()
    assert remote.exists(handoff.final_base_path)
    assert captured.value.torrent_removed is None  # type: ignore[attr-defined]
    assert not any(call[0] == "remove_tree" for call in remote.calls)


def test_remote_delete_failure_reports_partial_cleanup(tmp_path: Path) -> None:
    source = "/downloads/1091-the-thing-aaaaaaaa/The Thing"
    remote = RemovalFailureFilesystem(source)
    service, handoff, remote, client, movies = prepare(tmp_path, filesystem=remote)
    remote.add_file(f"{source}/movie.mkv", size=4)

    with pytest.raises(TransferCleanupError) as captured:
        service.run(handoff)

    assert (movies / "The Thing" / "movie.mkv").is_file()
    assert client.present is False
    assert captured.value.torrent_removed is True  # type: ignore[attr-defined]
    assert source in captured.value.remaining_paths  # type: ignore[attr-defined]


def test_missing_owner_or_incomplete_torrent_prevents_transfer(tmp_path: Path) -> None:
    wrong_owner = FakeTransferClient(
        "/downloads/1091-the-thing-aaaaaaaa/The Thing",
        custom={
            "media_download_job_id": "another-job",
            "media_download_state": "READY_FOR_TRANSFER",
        },
    )
    service, handoff, remote, _client, _movies = prepare(tmp_path, client=wrong_owner)
    remote.add_file(handoff.final_base_path / "movie.mkv")
    with pytest.raises(Exception, match="different media job"):
        service.run(handoff)

    incomplete = FakeTransferClient(str(handoff.final_base_path), complete=False)
    service, handoff, _remote, _client, _movies = prepare(tmp_path / "other", client=incomplete)
    with pytest.raises(Exception, match="verified completion"):
        service.run(handoff)
