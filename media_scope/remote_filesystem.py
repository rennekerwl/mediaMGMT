"""SFTP-backed seedbox filesystem operations used by the local Step 6 controller."""

from __future__ import annotations

import errno
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from media_scope.exceptions import DownloadStorageError, SeedboxFilesystemError


@dataclass(frozen=True, slots=True)
class RemotePathInfo:
    """Non-following metadata for one remote path."""

    path: PurePosixPath
    is_directory: bool
    is_file: bool
    is_symlink: bool
    size_bytes: int = 0


class RemoteFilesystem(Protocol):
    """Operations Step 6 needs without assuming a locally mounted seedbox."""

    def close(self) -> None: ...

    def home(self) -> PurePosixPath: ...

    def exists(self, path: PurePosixPath) -> bool: ...

    def canonicalize(self, path: PurePosixPath) -> PurePosixPath: ...

    def lstat(self, path: PurePosixPath) -> RemotePathInfo: ...

    def mkdirs(self, path: PurePosixPath) -> None: ...

    def listdir(self, path: PurePosixPath) -> list[RemotePathInfo]: ...

    def tree_size(self, path: PurePosixPath) -> int: ...


class SftpRemoteFilesystem:
    """Lazy, host-key-verified SFTP client with reconnect-on-next-operation behavior."""

    protocol = "sftp"

    def __init__(
        self,
        host: str,
        *,
        port: int,
        username: str,
        password: str,
        timeout_seconds: float = 15,
        known_hosts: Path | None = None,
    ) -> None:
        if not host or not username or not password:
            error = SeedboxFilesystemError(
                "SEEDBOX_SSH_HOST, SEEDBOX_USERNAME, and SEEDBOX_PASSWORD are required for SFTP."
            )
            error.error_code = "SFTP_CONFIGURATION_ERROR"
            raise error
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.known_hosts = known_hosts or (Path.home() / ".ssh" / "known_hosts")
        self._client: object | None = None
        self._sftp: object | None = None

    def __enter__(self) -> SftpRemoteFilesystem:
        self._ensure_connected()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        sftp, client = self._sftp, self._client
        self._sftp = None
        self._client = None
        if sftp is not None:
            try:
                sftp.close()  # type: ignore[union-attr]
            except Exception:
                pass
        if client is not None:
            try:
                client.close()  # type: ignore[union-attr]
            except Exception:
                pass

    def home(self) -> PurePosixPath:
        return self._path(self._call(lambda sftp: sftp.normalize(".")))

    def exists(self, path: PurePosixPath) -> bool:
        try:
            self.lstat(path)
        except FileNotFoundError:
            return False
        return True

    def canonicalize(self, path: PurePosixPath) -> PurePosixPath:
        return self._path(self._call(lambda sftp: sftp.normalize(str(path))))

    def lstat(self, path: PurePosixPath) -> RemotePathInfo:
        attributes = self._call(lambda sftp: sftp.lstat(str(path)))
        mode = int(attributes.st_mode)
        return RemotePathInfo(
            path=path,
            is_directory=stat_module.S_ISDIR(mode),
            is_file=stat_module.S_ISREG(mode),
            is_symlink=stat_module.S_ISLNK(mode),
            size_bytes=int(getattr(attributes, "st_size", 0) or 0),
        )

    def mkdirs(self, path: PurePosixPath) -> None:
        current = PurePosixPath("/")
        for component in path.parts[1:]:
            current /= component
            if self.exists(current):
                if not self.lstat(current).is_directory:
                    self._storage_error(
                        "The remote download path contains a non-directory component."
                    )
                continue
            try:
                self._call(
                    lambda sftp, current=current: sftp.mkdir(str(current), mode=0o700), retry=False
                )
            except FileExistsError:
                if not self.exists(current) or not self.lstat(current).is_directory:
                    raise

    def listdir(self, path: PurePosixPath) -> list[RemotePathInfo]:
        attributes = self._call(lambda sftp: sftp.listdir_attr(str(path)))
        result: list[RemotePathInfo] = []
        for item in attributes:
            child = path / str(item.filename)
            mode = int(item.st_mode)
            result.append(
                RemotePathInfo(
                    path=child,
                    is_directory=stat_module.S_ISDIR(mode),
                    is_file=stat_module.S_ISREG(mode),
                    is_symlink=stat_module.S_ISLNK(mode),
                    size_bytes=int(getattr(item, "st_size", 0) or 0),
                )
            )
        return sorted(result, key=lambda item: item.path.name)

    def tree_size(self, path: PurePosixPath) -> int:
        info = self.lstat(path)
        if info.is_symlink:
            return 0
        if info.is_file:
            return info.size_bytes
        if not info.is_directory:
            return 0
        return sum(
            0 if child.is_symlink else self.tree_size(child.path) for child in self.listdir(path)
        )

    def _ensure_connected(self) -> object:
        if self._sftp is not None:
            return self._sftp
        try:
            import paramiko
        except ImportError as exc:
            error = SeedboxFilesystemError("Paramiko is required for Step 6 SFTP operations.")
            error.error_code = "SFTP_CONFIGURATION_ERROR"
            raise error from exc
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.known_hosts.exists():
            client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout_seconds,
                banner_timeout=self.timeout_seconds,
                auth_timeout=self.timeout_seconds,
                look_for_keys=False,
                allow_agent=False,
            )
            self._client = client
            self._sftp = client.open_sftp()
            return self._sftp
        except paramiko.AuthenticationException as exc:
            client.close()
            error = SeedboxFilesystemError("SFTP authentication was rejected by the seedbox.")
            error.error_code = "SFTP_AUTHENTICATION_FAILED"
            raise error from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            error = SeedboxFilesystemError("The seedbox SSH host key did not match known_hosts.")
            error.error_code = "SFTP_HOST_KEY_ERROR"
            raise error from exc
        except paramiko.SSHException as exc:
            client.close()
            message = str(exc).casefold()
            error = SeedboxFilesystemError(
                "The seedbox SSH host key is not trusted by the configured known_hosts file."
                if "known_hosts" in message or "host key" in message
                else "The seedbox SFTP service could not be reached safely."
            )
            error.error_code = (
                "SFTP_HOST_KEY_ERROR"
                if "known_hosts" in message or "host key" in message
                else "SFTP_UNAVAILABLE"
            )
            raise error from exc
        except Exception as exc:
            client.close()
            error = SeedboxFilesystemError("The seedbox SFTP service could not be reached safely.")
            error.error_code = "SFTP_UNAVAILABLE"
            raise error from exc

    def _call(self, operation: object, *, retry: bool = True) -> object:
        """Run a read operation once more after an idle SFTP session disconnects.

        Directory creation opts out: its result is checked by ``mkdirs`` before any
        later action, so an ambiguous network failure never blindly repeats a write.
        """
        for attempt in range(2 if retry else 1):
            try:
                return operation(self._ensure_connected())  # type: ignore[operator]
            except FileExistsError:
                raise
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                    raise FileNotFoundError(str(exc)) from exc
                self.close()
                self._storage_error("The seedbox rejected a required remote path operation.")
            except SeedboxFilesystemError:
                raise
            except Exception as exc:
                self.close()
                if retry and attempt == 0:
                    continue
                error = SeedboxFilesystemError(
                    "The seedbox SFTP session failed during a remote operation."
                )
                error.error_code = "SFTP_UNAVAILABLE"
                raise error from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _path(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if not path.is_absolute() or any(part == ".." for part in path.parts):
            raise ValueError("SFTP returned an unsafe remote path")
        return path

    @staticmethod
    def _storage_error(message: str) -> None:
        error = DownloadStorageError(message)
        error.error_code = "SFTP_PATH_OPERATION_FAILED"
        raise error
