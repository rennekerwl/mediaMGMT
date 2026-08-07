"""In-memory SFTP-shaped filesystem for Step 6 tests."""

from __future__ import annotations

from pathlib import PurePosixPath

from media_scope.remote_filesystem import RemotePathInfo


class FakeRemoteFilesystem:
    protocol = "sftp"

    def __init__(self, *, home: str = "/home/seedboxer1") -> None:
        self._home = PurePosixPath(home)
        self.entries: dict[PurePosixPath, RemotePathInfo] = {
            PurePosixPath("/"): RemotePathInfo(PurePosixPath("/"), True, False, False),
            self._home: RemotePathInfo(self._home, True, False, False),
        }
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> FakeRemoteFilesystem:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.calls.append(("close", ""))

    def home(self) -> PurePosixPath:
        self.calls.append(("home", ""))
        return self._home

    def exists(self, path: PurePosixPath) -> bool:
        self.calls.append(("exists", str(path)))
        return path in self.entries

    def canonicalize(self, path: PurePosixPath) -> PurePosixPath:
        self.calls.append(("canonicalize", str(path)))
        if path not in self.entries:
            raise FileNotFoundError(str(path))
        return path

    def lstat(self, path: PurePosixPath) -> RemotePathInfo:
        self.calls.append(("lstat", str(path)))
        try:
            return self.entries[path]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def mkdirs(self, path: PurePosixPath) -> None:
        self.calls.append(("mkdirs", str(path)))
        current = PurePosixPath("/")
        for component in path.parts[1:]:
            current /= component
            existing = self.entries.get(current)
            if existing is not None and not existing.is_directory:
                raise OSError("not a directory")
            self.entries.setdefault(current, RemotePathInfo(current, True, False, False))

    def listdir(self, path: PurePosixPath) -> list[RemotePathInfo]:
        self.calls.append(("listdir", str(path)))
        if not self.lstat(path).is_directory:
            raise OSError("not a directory")
        return sorted(
            [item for item in self.entries.values() if item.path.parent == path],
            key=lambda item: item.path.name,
        )

    def tree_size(self, path: PurePosixPath) -> int:
        self.calls.append(("tree_size", str(path)))
        info = self.lstat(path)
        if info.is_symlink:
            return 0
        if info.is_file:
            return info.size_bytes
        return sum(self.tree_size(item.path) for item in self.listdir(path) if not item.is_symlink)

    def add_file(self, path: str | PurePosixPath, *, size: int = 7) -> PurePosixPath:
        remote = PurePosixPath(path)
        self.mkdirs(remote.parent)
        self.entries[remote] = RemotePathInfo(remote, False, True, False, size)
        return remote

    def add_directory(self, path: str | PurePosixPath) -> PurePosixPath:
        remote = PurePosixPath(path)
        self.mkdirs(remote)
        return remote

    def add_symlink(self, path: str | PurePosixPath) -> PurePosixPath:
        remote = PurePosixPath(path)
        self.mkdirs(remote.parent)
        self.entries[remote] = RemotePathInfo(remote, False, False, True)
        return remote
