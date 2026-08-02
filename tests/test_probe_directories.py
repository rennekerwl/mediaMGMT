"""Remote probe-directory safety tests using an in-memory rTorrent command gateway."""

from __future__ import annotations

import pytest

from media_scope.exceptions import RtorrentProbeDirectoryError, RtorrentRpcFault
from media_scope.probe_directories import ProbeDirectoryManager


class FakeRemoteCommands:
    def __init__(self) -> None:
        self.directories = {"/home21/seedboxer1", "/home21/seedboxer1/media-probes"}
        self.aliases = {"/home/seedboxer1/media-probes": "/home21/seedboxer1/media-probes"}
        self.calls: list[tuple[object, ...]] = []

    def call(self, method: str, *params: object) -> object:
        self.calls.append((method, *params))
        if method == "system.cwd":
            return "/home21/seedboxer1"
        command = params[1]
        path = str(params[-1])
        if method == "execute.capture" and command == "/usr/bin/realpath":
            resolved = self.aliases.get(path, path)
            if resolved not in self.directories:
                raise RtorrentRpcFault("missing", fault_code=1)
            return f"{resolved}\n"
        if method == "execute.capture" and command == "/usr/bin/stat":
            return "directory\n"
        if method == "execute.throw" and command == "/usr/bin/test":
            if path not in self.directories:
                raise RtorrentRpcFault("not writable", fault_code=1)
            return 0
        if method == "execute.throw" and command == "/bin/mkdir":
            parent = path.rsplit("/", 1)[0]
            if parent not in self.directories:
                raise RtorrentRpcFault("missing parent", fault_code=1)
            self.directories.add(path)
            return 0
        if method == "execute.throw" and command == "/bin/rm":
            self.directories = {
                value
                for value in self.directories
                if value != path and not value.startswith(path + "/")
            }
            return 0
        if method == "execute.throw" and command == "/bin/rmdir":
            if any(value.startswith(path + "/") for value in self.directories):
                raise RtorrentRpcFault("not empty", fault_code=1)
            self.directories.discard(path)
            return 0
        raise AssertionError(f"unexpected command: {method} {params}")


def test_remote_manager_uses_canonical_posix_paths_and_removes_only_failed_candidate() -> None:
    client = FakeRemoteCommands()
    manager = ProbeDirectoryManager(client, "/home/seedboxer1/media-probes", "probe-test")

    manager.prepare_job()
    candidate = manager.prepare_candidate("a" * 40)
    manager.cleanup_candidate(candidate)
    manager.cleanup_empty_job()

    assert candidate == "/home21/seedboxer1/media-probes/probe-test/" + "a" * 40
    assert client.directories == {"/home21/seedboxer1", "/home21/seedboxer1/media-probes"}
    assert (
        "execute.throw",
        "",
        "/bin/rm",
        "-rf",
        "--",
        candidate,
    ) in client.calls


@pytest.mark.parametrize("root", ["", "/", "C:\\probe", "/home/../etc", "/home/probe\n"])
def test_remote_manager_rejects_unsafe_or_non_posix_roots(root: str) -> None:
    with pytest.raises(RtorrentProbeDirectoryError):
        ProbeDirectoryManager(FakeRemoteCommands(), root, "probe-test")


def test_cleanup_refuses_a_path_outside_the_canonical_root() -> None:
    client = FakeRemoteCommands()
    client.directories.add("/outside")
    manager = ProbeDirectoryManager(client, "/home/seedboxer1/media-probes", "probe-test")
    manager.prepare_job()

    with pytest.raises(RtorrentProbeDirectoryError):
        manager.cleanup_candidate("/outside")

    assert not any(
        call[2:4] == ("/bin/rm", "-rf") and call[-1] == "/outside" for call in client.calls
    )
