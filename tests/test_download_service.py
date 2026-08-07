"""Mocked state-machine tests for Step 6 downloads."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from media_scope.download_directories import DownloadDirectoryManager
from media_scope.download_input import parse_download_input
from media_scope.download_models import DownloadCapabilities, DownloadSnapshot
from media_scope.download_service import DownloadPolicy, TorrentDownloadService
from tests.fake_remote_filesystem import FakeRemoteFilesystem
from tests.test_download_input import HASH_A, health_result


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.epoch = datetime(2026, 7, 31, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def wall(self) -> datetime:
        return self.epoch + timedelta(seconds=self.value)


class FakeDownloadClient:
    sanitized_endpoint = "http://rtorrent.test/RPC"

    def __init__(
        self,
        snapshots: list[DownloadSnapshot],
        *,
        custom: dict[str, str] | None = None,
        exists: list[bool] | None = None,
    ) -> None:
        self.snapshots = list(snapshots)
        self.last = snapshots[-1]
        self.custom = dict(custom or {})
        self.exists_values = list(exists or [])
        self.calls: list[tuple[Any, ...]] = []

    def discover_download_capabilities(self) -> DownloadCapabilities:
        return DownloadCapabilities("0.9.8", "0.13.8", "9", frozenset(), "d.is_meta", "d.hash")

    def torrent_exists(self, infohash: str) -> bool:
        self.calls.append(("exists", infohash))
        return self.exists_values.pop(0) if self.exists_values else True

    def download_snapshot(self, infohash: str) -> DownloadSnapshot:
        self.calls.append(("snapshot", infohash))
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return self.last

    def get_custom(self, infohash: str, name: str) -> str | None:
        return self.custom.get(name)

    def set_download_directory(self, infohash: str, directory: Path) -> None:
        self.calls.append(("directory", infohash, str(directory)))
        self.last = replace(self.last, directory=str(directory))

    def tag_download(self, infohash: str, **values: Any) -> None:
        self.calls.append(("tag", infohash, values))
        self.custom["media_download_job_id"] = str(values["job_id"])

    def set_download_state(self, infohash: str, state: str) -> None:
        self.calls.append(("custom_state", infohash, state))
        self.custom["media_download_state"] = state

    def start(self, infohash: str) -> None:
        self.calls.append(("start", infohash))

    def stop(self, infohash: str) -> None:
        self.calls.append(("stop", infohash))


def remote_path(path: Path | PurePosixPath) -> PurePosixPath:
    if isinstance(path, PurePosixPath):
        return path
    names = list(path.parts)
    for root in ("downloads", "library", "outside", "probes"):
        if root in names:
            return PurePosixPath("/", *names[names.index(root) :])
    return PurePosixPath("/downloads", path.name)


def snapshot(
    directory: Path | PurePosixPath,
    *,
    completed: int = 0,
    size: int = 100,
    active: bool = True,
    complete: bool = False,
    rate: int = 10,
    peers: int = 2,
    message: str | None = None,
    hashing: bool = False,
    base_path: Path | PurePosixPath | None = None,
) -> DownloadSnapshot:
    return DownloadSnapshot(
        infohash=HASH_A,
        name="Example Complete",
        metadata_retrieved=True,
        state=1 if active else 0,
        is_active=active,
        is_open=active,
        complete=complete,
        completed_bytes=completed,
        size_bytes=size,
        left_bytes=max(size - completed, 0),
        download_rate=rate,
        upload_rate=0,
        uploaded_bytes=12,
        connected_peers=peers,
        complete_peers=1,
        message=message,
        base_path=str(remote_path(base_path or directory / "payload")),
        directory=str(remote_path(directory)),
        ratio=0,
        hashing=hashing,
    )


def make_service(
    tmp_path: Path,
    client: FakeDownloadClient,
    *,
    stall: float = 10,
    timeout: float = 0,
    grace: float = 0,
    post_policy: str = "stop",
    allowed: list[Path] | None = None,
) -> tuple[TorrentDownloadService, DownloadDirectoryManager, Clock]:
    root = PurePosixPath("/downloads")
    filesystem = FakeRemoteFilesystem()
    directories = DownloadDirectoryManager(
        filesystem,
        root,
        tmdb_id=4608,
        title="30 Rock",
        infohash=HASH_A,
        allowed_final_roots=[remote_path(item) for item in allowed or []],
    )
    clock = Clock()
    service = TorrentDownloadService(
        client,  # type: ignore[arg-type]
        directories,
        DownloadPolicy(
            poll_interval_seconds=1,
            stall_timeout_seconds=stall,
            overall_timeout_seconds=timeout,
            post_completion_policy=post_policy,
            post_processing_grace_seconds=grace,
        ),
        job_id="download-4608-aaaaaaaaaaaa",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.wall,
    )
    return service, directories, clock


def run(service: TorrentDownloadService, **kwargs: Any) -> tuple[dict[str, Any], int]:
    return service.run(parse_download_input(health_result()), **kwargs)


def prepare_payload(directories: DownloadDirectoryManager, name: str = "payload") -> PurePosixPath:
    path = directories.job_directory / name
    directories.filesystem.add_file(path / "episode.mkv", size=7)  # type: ignore[attr-defined]
    return path


def test_normal_progress_completes_stops_and_retains_torrent(tmp_path: Path) -> None:
    seed_dir = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(seed_dir, active=False, rate=0),
            snapshot(seed_dir, completed=25),
            snapshot(seed_dir, completed=100, complete=True, rate=0),
        ]
    )
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["status"] == "READY_FOR_TRANSFER"
    assert payload["download"]["status"] == "DOWNLOAD_COMPLETED"
    assert payload["paths"]["payload_size_bytes"] == 7
    assert ("start", HASH_A) in client.calls
    assert ("stop", HASH_A) in client.calls
    assert not any(call[0] in {"submit", "erase"} for call in client.calls)


def test_active_restart_resumes_monitoring_without_duplicate_start(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(target, completed=40),
            snapshot(target, completed=100, complete=True, rate=0),
        ],
        custom={"media_download_job_id": "download-4608-aaaaaaaaaaaa"},
    )
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["ready_for_transfer"] is True
    assert not any(call[0] == "start" for call in client.calls)


def test_missing_torrent_and_metadata_only_are_identity_failures(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    missing = FakeDownloadClient([snapshot(target)], exists=[False])
    payload, code = run(make_service(tmp_path, missing)[0])
    assert (code, payload["error_code"]) == (3, "SELECTED_TORRENT_NOT_FOUND")

    meta = FakeDownloadClient([replace(snapshot(target), metadata_retrieved=False, size_bytes=0)])
    payload, code = run(make_service(tmp_path / "meta", meta)[0])
    assert (code, payload["error_code"]) == (3, "TORRENT_STILL_META")


def test_rtorrent_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient([replace(snapshot(target), infohash="b" * 40)])
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (3, "INFOHASH_MISMATCH")


def test_partial_probe_payload_requires_safe_relocation(tmp_path: Path) -> None:
    probe = (tmp_path / "probes" / "probe-job" / HASH_A).resolve()
    client = FakeDownloadClient([snapshot(probe, completed=1, active=False, rate=0)])
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (5, "PROBE_DATA_RELOCATION_REQUIRED")
    assert not any(call[0] == "directory" for call in client.calls)


def test_empty_probe_directory_is_redirected_and_confirmed_before_start(tmp_path: Path) -> None:
    probe = (tmp_path / "probes" / "probe-job" / HASH_A).resolve()
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(probe, active=False, rate=0),
            snapshot(target, active=False, rate=0),
            snapshot(target, completed=100, complete=True, rate=0, base_path=target),
        ]
    )
    service, directories, _clock = make_service(tmp_path, client)
    payload, code = run(service)
    assert code == 0
    directory_call = next(
        index for index, call in enumerate(client.calls) if call[0] == "directory"
    )
    start_call = next(index for index, call in enumerate(client.calls) if call[0] == "start")
    assert directory_call < start_call
    assert (
        payload["paths"]["base_path_before_download"] == "/downloads/4608-30-rock-aaaaaaaa/payload"
    )


@pytest.mark.parametrize(
    ("peers", "reason"),
    [(0, "NO_CONNECTED_PEERS"), (3, "CONNECTED_BUT_NO_PROGRESS")],
)
def test_stall_classification_and_peer_changes_do_not_reset_progress(
    tmp_path: Path, peers: int, reason: str
) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    peer_samples = [0, 0, 0] if peers == 0 else [peers, peers + 1, peers + 2]
    client = FakeDownloadClient(
        [
            snapshot(target, active=False, rate=0, peers=peers),
            *(snapshot(target, rate=0, peers=value) for value in peer_samples),
        ]
    )
    payload, code = run(make_service(tmp_path, client, stall=2)[0])
    assert (code, payload["error_code"]) == (6, "DOWNLOAD_STALLED")
    assert payload["diagnostics"]["stall_reason"] == reason


def test_tracker_message_is_classified_on_stall(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    message = "Tracker request timed out"
    client = FakeDownloadClient(
        [snapshot(target, active=False, rate=0), snapshot(target, rate=0, message=message)]
    )
    payload, code = run(make_service(tmp_path, client, stall=0.0)[0])
    assert code == 6
    assert payload["diagnostics"]["stall_reason"] == "TRACKER_OR_NETWORK_ERROR"


def test_terminal_rtorrent_error_stops_immediately(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [snapshot(target, active=False, rate=0), snapshot(target, message="Permission denied")]
    )
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (3, "RTORRENT_REPORTED_ERROR")


def test_hash_check_failure_has_distinct_error(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [snapshot(target, active=False, rate=0), snapshot(target, message="Hash check failed")]
    )
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (3, "HASH_CHECK_FAILED")


def test_torrent_disappearance_during_monitoring_is_reported(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient([snapshot(target, active=False, rate=0)], exists=[True, False])
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (3, "TORRENT_DISAPPEARED")


def test_timeout_stops_and_returns_exit_seven(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [snapshot(target, active=False, rate=0), snapshot(target, rate=0), snapshot(target, rate=0)]
    )
    payload, code = run(make_service(tmp_path, client, stall=10, timeout=1)[0])
    assert (code, payload["error_code"]) == (7, "DOWNLOAD_TIMEOUT")


def test_useful_byte_progress_resets_stall_timer(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(target, active=False, rate=0),
            snapshot(target, completed=0, rate=0),
            snapshot(target, completed=20),
            snapshot(target, completed=20, rate=0),
            snapshot(target, completed=100, complete=True, rate=0),
        ]
    )
    service, directories, _clock = make_service(tmp_path, client, stall=2)
    prepare_payload(directories)
    _payload, code = run(service)
    assert code == 0


def test_hash_check_must_finish_before_completion(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(target, active=False, rate=0),
            snapshot(target, completed=100, complete=True, rate=0, hashing=True),
            snapshot(target, completed=100, complete=True, rate=0, hashing=False),
        ]
    )
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    states = [item["state"] for item in payload["state_transitions"]]
    assert "HASH_CHECKING" in states


def test_hash_check_already_active_is_monitored_without_start(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [
            snapshot(
                target,
                completed=100,
                complete=True,
                rate=0,
                hashing=True,
                active=False,
            ),
            snapshot(target, completed=100, complete=True, rate=0, hashing=False),
        ]
    )
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["download"]["status"] == "DOWNLOAD_COMPLETED"
    assert not any(call[0] == "start" for call in client.calls)


def test_already_complete_returns_success_without_start(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient([snapshot(target, completed=100, complete=True, rate=0)])
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["download"]["status"] == "DOWNLOAD_COMPLETED"
    assert not any(call[0] == "start" for call in client.calls)


def test_prior_stall_requires_explicit_resume(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    custom = {
        "media_download_job_id": "download-4608-aaaaaaaaaaaa",
        "media_download_state": "STALLED",
    }
    client = FakeDownloadClient([snapshot(target, active=False, rate=0)], custom=custom)
    payload, code = run(make_service(tmp_path, client)[0])
    assert (code, payload["error_code"]) == (6, "DOWNLOAD_STALLED")
    assert not any(call[0] == "start" for call in client.calls)


def test_resume_stalled_flag_allows_validated_restart(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    custom = {
        "media_download_job_id": "download-4608-aaaaaaaaaaaa",
        "media_download_state": "STALLED",
    }
    client = FakeDownloadClient(
        [
            snapshot(target, active=False, rate=0),
            snapshot(target, completed=100, complete=True, rate=0),
        ],
        custom=custom,
    )
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service, resume_stalled=True)
    assert code == 0
    assert payload["ready_for_transfer"] is True
    assert ("start", HASH_A) in client.calls


def test_leave_running_policy_starts_completed_torrent_for_seeding(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient(
        [snapshot(target, completed=100, complete=True, rate=0, active=False)]
    )
    service, directories, _clock = make_service(tmp_path, client, post_policy="leave_running")
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["rtorrent"]["final_state"] == "seeding"
    assert ("start", HASH_A) in client.calls
    assert not any(call[0] == "stop" for call in client.calls)


def test_completion_hook_path_move_to_allowed_root_is_detected(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    allowed = (tmp_path / "library").resolve()
    moved = allowed / "30 Rock"
    moved.mkdir(parents=True)
    (moved / "episode.mkv").write_bytes(b"done")
    before = snapshot(target, completed=100, complete=True, rate=0)
    after = snapshot(target, completed=100, complete=True, rate=0, base_path=moved)
    client = FakeDownloadClient([before, after])
    service, directories, _clock = make_service(tmp_path, client, grace=1, allowed=[allowed])
    directories.filesystem.add_file("/library/30 Rock/episode.mkv", size=4)  # type: ignore[attr-defined]
    payload, code = run(service)
    assert code == 0
    assert payload["paths"]["final_base_path"] == "/library/30 Rock"
    assert payload["paths"]["path_changed_after_completion"] is True


def test_completion_hook_path_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    outside = (tmp_path / "outside" / "payload").resolve()
    outside.mkdir(parents=True)
    client = FakeDownloadClient(
        [snapshot(target, completed=100, complete=True, rate=0, base_path=outside)]
    )
    service, directories, _clock = make_service(tmp_path, client)
    directories.filesystem.add_directory("/outside/payload")  # type: ignore[attr-defined]
    payload, code = run(service)
    assert (code, payload["error_code"]) == (8, "FINAL_PATH_OUTSIDE_ALLOWED_ROOT")
    assert payload["ready_for_transfer"] is False
    assert ("stop", HASH_A) in client.calls


def test_unchanged_completion_path_is_reported(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient([snapshot(target, completed=100, complete=True, rate=0)])
    service, directories, _clock = make_service(tmp_path, client)
    prepare_payload(directories)
    payload, code = run(service)
    assert code == 0
    assert payload["paths"]["path_changed_after_completion"] is False


def test_dry_run_performs_no_mutating_rpc_calls(tmp_path: Path) -> None:
    target = (tmp_path / "downloads" / "4608-30-rock-aaaaaaaa").resolve()
    client = FakeDownloadClient([snapshot(target, active=False, rate=0)])
    payload, code = run(make_service(tmp_path, client)[0], dry_run=True)
    assert code == 0
    assert payload["result"] == "dry_run"
    assert payload["mutations_performed"] is False
    assert not any(
        call[0] in {"start", "stop", "directory", "tag", "custom_state"} for call in client.calls
    )
