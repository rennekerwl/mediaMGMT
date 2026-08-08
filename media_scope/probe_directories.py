"""Conservative rTorrent-host directory ownership and cleanup for live probes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

from media_scope.exceptions import (
    DownloadError,
    RtorrentConfigurationError,
    RtorrentProbeDirectoryError,
    RtorrentRpcFault,
)
from media_scope.remote_filesystem import RemoteFilesystem


class RemoteCommandClient(Protocol):
    """The narrow rTorrent RPC surface needed for remote probe-directory management."""

    def call(self, method: str, *params: object) -> object: ...


class ProbeDirectoryManager:
    """Create and remove only generated paths beneath a seedbox-side probe root."""

    def __init__(self, client: RemoteCommandClient, root: str, job_id: str) -> None:
        self.client = client
        self.root = _validate_root(root)
        if not job_id.startswith("probe-") or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in job_id
        ):
            raise RtorrentProbeDirectoryError("Generated probe job ID is not path-safe.")
        self.job_id = job_id
        self.canonical_root: PurePosixPath | None = None

    @property
    def job_directory(self) -> PurePosixPath:
        return self._root() / self.job_id

    def check_root(self) -> None:
        """Verify the configured, pre-created remote root without mutating it."""
        root = self._resolve_existing(self.root)
        try:
            working_directory = self._resolve_existing(
                _validate_path(str(self.client.call("system.cwd")).strip())
            )
        except (RtorrentProbeDirectoryError, RtorrentRpcFault, ValueError) as exc:
            raise RtorrentProbeDirectoryError(
                "rTorrent could not report a safe working directory for probe-root validation."
            ) from exc
        if (
            root
            in {
                PurePosixPath("/"),
                PurePosixPath("/home"),
                PurePosixPath("/srv"),
                PurePosixPath("/var"),
                PurePosixPath("/tmp"),
            }
            or root == working_directory
        ):
            raise RtorrentProbeDirectoryError(
                "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
            )
        self._stat_directory(root)
        try:
            self.client.call("execute.throw", "", "/usr/bin/test", "-w", str(root))
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "RTORRENT_PROBE_DIRECTORY is not writable by the rTorrent process."
            ) from exc
        self.canonical_root = root

    def prepare_job(self) -> None:
        """Create the unique job directory beneath the validated remote root."""
        self.check_root()
        try:
            self.client.call(
                "execute.throw", "", "/bin/mkdir", "--mode=700", "--", str(self.job_directory)
            )
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "The dedicated rTorrent probe job directory could not be created."
            ) from exc
        self._assert_owned(self._resolve_existing(self.job_directory))

    def prepare_candidate(self, label: str) -> str:
        """Create one injection-safe child directory for an owned candidate."""
        if not label or any(character not in "0123456789abcdefABCDEF-_" for character in label):
            raise RtorrentProbeDirectoryError("Probe child-directory label is unsafe.")
        path = self.job_directory / label
        try:
            self.client.call("execute.throw", "", "/bin/mkdir", "--mode=700", "--", str(path))
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "The dedicated rTorrent candidate directory could not be created."
            ) from exc
        resolved = self._resolve_existing(path)
        self._assert_owned(resolved)
        return str(resolved)

    def cleanup_candidate(self, path: str) -> None:
        """Remove exactly one verified owned candidate directory, if present."""
        try:
            candidate = _validate_path(path)
        except ValueError as exc:
            raise RtorrentProbeDirectoryError("Probe candidate cleanup path is unsafe.") from exc
        try:
            resolved = self._resolve_existing(candidate)
        except RtorrentProbeDirectoryError:
            return
        self._assert_owned(resolved)
        if resolved == self.job_directory:
            raise RtorrentProbeDirectoryError(
                "Refusing candidate cleanup of the whole job directory."
            )
        try:
            self.client.call("execute.throw", "", "/bin/rm", "-rf", "--", str(resolved))
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "The dedicated rTorrent candidate directory could not be removed."
            ) from exc

    def cleanup_empty_job(self) -> None:
        """Remove the unique job directory only when it is empty."""
        if self.canonical_root is None:
            return
        self._assert_owned(self.job_directory)
        try:
            self.client.call("execute.throw", "", "/bin/rmdir", "--", str(self.job_directory))
        except RtorrentRpcFault:
            pass

    def _root(self) -> PurePosixPath:
        if self.canonical_root is None:
            raise RtorrentProbeDirectoryError("The remote probe directory has not been verified.")
        return self.canonical_root

    def _resolve_existing(self, path: PurePosixPath) -> PurePosixPath:
        try:
            value = self.client.call("execute.capture", "", "/usr/bin/realpath", str(path))
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "RTORRENT_PROBE_DIRECTORY or one of its generated paths is missing "
                "on rTorrent's host."
            ) from exc
        try:
            return _validate_path(str(value).strip())
        except ValueError as exc:
            raise RtorrentProbeDirectoryError(
                "rTorrent returned an unsafe probe-directory path."
            ) from exc

    def _stat_directory(self, path: PurePosixPath) -> None:
        try:
            value = self.client.call("execute.capture", "", "/usr/bin/stat", "-c", "%F", str(path))
        except RtorrentRpcFault as exc:
            raise RtorrentProbeDirectoryError(
                "RTORRENT_PROBE_DIRECTORY could not be inspected on rTorrent's host."
            ) from exc
        if str(value).strip() != "directory":
            raise RtorrentProbeDirectoryError("RTORRENT_PROBE_DIRECTORY must name a directory.")

    def _assert_owned(self, path: PurePosixPath) -> None:
        try:
            path.relative_to(self._root())
        except ValueError as exc:
            raise RtorrentProbeDirectoryError(
                "Refusing filesystem operation outside RTORRENT_PROBE_DIRECTORY."
            ) from exc
        if path == self._root():
            raise RtorrentProbeDirectoryError("Refusing to operate on the shared probe root.")


def _validate_root(value: str) -> PurePosixPath:
    try:
        path = _validate_path(value)
    except ValueError as exc:
        raise RtorrentProbeDirectoryError(
            "RTORRENT_PROBE_DIRECTORY must be an absolute POSIX path on rTorrent's host."
        ) from exc
    if path == PurePosixPath("/"):
        raise RtorrentProbeDirectoryError(
            "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
        )
    return path


def _validate_path(value: str) -> PurePosixPath:
    if not value or any(character in value for character in ("\x00", "\n", "\r", ",")):
        raise ValueError("unsafe path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("path is not absolute and normalized")
    return path


class RemoteProbeDirectoryManager:
    """Create and clean probe directories on a seedbox through SFTP."""

    def __init__(
        self,
        filesystem: RemoteFilesystem,
        root: str | PurePosixPath,
        job_id: str,
    ) -> None:
        self.filesystem = filesystem
        self.root = self._remote_path(root)
        if self.root == PurePosixPath("/"):
            raise RtorrentConfigurationError(
                "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
            )
        if not job_id.startswith("probe-") or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in job_id
        ):
            raise RtorrentConfigurationError("Generated probe job ID is not path-safe.")
        self.job_directory = self.root / job_id
        self._assert_owned(self.job_directory)

    def prepare_job(self) -> None:
        """Create a unique remote job directory beneath the configured probe root."""
        try:
            home = self.filesystem.home()
            if self.root == home or self.root in home.parents:
                raise RtorrentConfigurationError(
                    "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
                )
            requested_root = self.root
            existing = requested_root
            missing_parts: list[str] = []
            while not self.filesystem.exists(existing):
                if existing == PurePosixPath("/"):
                    raise RtorrentConfigurationError(
                        "RTORRENT_PROBE_DIRECTORY has no accessible remote ancestor."
                    )
                missing_parts.append(existing.name)
                existing = existing.parent
            canonical_parent = self.filesystem.canonicalize(existing)
            self.root = canonical_parent.joinpath(*reversed(missing_parts))
            self.filesystem.mkdirs(self.root)
            canonical_root = self.filesystem.canonicalize(self.root)
            if canonical_root in {PurePosixPath("/"), home} or canonical_root in home.parents:
                raise RtorrentConfigurationError(
                    "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
                )
            self.root = canonical_root
            self.job_directory = self.root / self.job_directory.name
            self._assert_owned(self.job_directory)
            if self.filesystem.exists(self.job_directory):
                raise RtorrentConfigurationError(
                    "The generated remote probe job directory already exists."
                )
            self.filesystem.mkdirs(self.job_directory)
            info = self.filesystem.lstat(self.job_directory)
            if not info.is_directory or info.is_symlink:
                raise RtorrentConfigurationError(
                    "The remote probe job path is not a safe directory."
                )
        except DownloadError as exc:
            raise RtorrentConfigurationError(
                "The remote rTorrent probe directory could not be prepared through SFTP."
            ) from exc

    def prepare_candidate(self, label: str) -> PurePosixPath:
        """Create one injection-safe remote candidate directory."""
        if not label or any(character not in "0123456789abcdefABCDEF-_" for character in label):
            raise RtorrentConfigurationError("Probe child-directory label is unsafe.")
        path = self.job_directory / label
        self._assert_owned(path)
        try:
            if self.filesystem.exists(path):
                raise RtorrentConfigurationError("The remote probe candidate path already exists.")
            self.filesystem.mkdirs(path)
            info = self.filesystem.lstat(path)
            if not info.is_directory or info.is_symlink:
                raise RtorrentConfigurationError(
                    "The remote probe candidate path is not a safe directory."
                )
        except DownloadError as exc:
            raise RtorrentConfigurationError(
                "The remote rTorrent candidate directory could not be prepared through SFTP."
            ) from exc
        return path

    def cleanup_candidate(self, path: Path | PurePosixPath) -> None:
        """Remove exactly one verified owned remote candidate tree."""
        remote = self._remote_path(str(path))
        self._assert_owned(remote)
        if remote == self.job_directory:
            raise RtorrentConfigurationError(
                "Refusing candidate cleanup of the whole remote job directory."
            )
        try:
            if self.filesystem.exists(remote):
                self.filesystem.remove_tree(remote)
        except DownloadError as exc:
            raise RtorrentConfigurationError(
                "The remote rTorrent candidate directory could not be cleaned through SFTP."
            ) from exc

    def cleanup_empty_job(self) -> None:
        """Remove the remote job directory only when it is empty."""
        self._assert_owned(self.job_directory)
        try:
            if self.filesystem.exists(self.job_directory) and not self.filesystem.listdir(
                self.job_directory
            ):
                self.filesystem.remove_tree(self.job_directory)
        except DownloadError as exc:
            raise RtorrentConfigurationError(
                "The empty remote rTorrent probe job directory could not be cleaned through SFTP."
            ) from exc

    @staticmethod
    def _remote_path(value: str | PurePosixPath) -> PurePosixPath:
        text = str(value)
        if not text or any(character in text for character in ("\x00", "\n", "\r", "\\")):
            raise RtorrentConfigurationError("RTORRENT_PROBE_DIRECTORY is unsafe.")
        path = PurePosixPath(text)
        if not path.is_absolute() or any(part == ".." for part in path.parts):
            raise RtorrentConfigurationError(
                "RTORRENT_PROBE_DIRECTORY must be an absolute POSIX path."
            )
        return path

    def _assert_owned(self, path: PurePosixPath) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RtorrentConfigurationError(
                "Refusing filesystem operation outside RTORRENT_PROBE_DIRECTORY."
            ) from exc
        if path == self.root:
            raise RtorrentConfigurationError("Refusing to operate on the remote probe root.")
