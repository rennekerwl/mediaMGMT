"""Safe remote permanent-directory and final-path handling for Step 6."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from media_scope.exceptions import DownloadPostProcessingError, DownloadStorageError
from media_scope.remote_filesystem import RemoteFilesystem

_SAFE_COMPONENT = re.compile(r"[^a-z0-9]+")


class DownloadDirectoryManager:
    """Validate and prepare deterministic paths on the seedbox through SFTP."""

    def __init__(
        self,
        filesystem: RemoteFilesystem,
        root: str | PurePosixPath,
        *,
        tmdb_id: int | str,
        title: str,
        infohash: str,
        probe_root: str | PurePosixPath | None = None,
        allowed_final_roots: Iterable[str | PurePosixPath] = (),
    ) -> None:
        self.filesystem = filesystem
        self.root = self._remote_path(root, "RTORRENT_DOWNLOAD_DIRECTORY")
        self._validate_root(self.root)
        self.probe_root = (
            self._remote_path(probe_root, "RTORRENT_PROBE_DIRECTORY")
            if probe_root is not None
            else None
        )
        if self.probe_root is not None and (
            _contains(self.root, self.probe_root) or _contains(self.probe_root, self.root)
        ):
            self._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", "Probe and permanent download directories must not overlap."
            )
        slug = sanitize_component(title)
        tmdb = sanitize_component(str(tmdb_id)) or "unknown"
        self._child = f"{tmdb}-{slug}-{infohash[:8].lower()}"
        self._assert_under(self.job_directory, self.root)
        self.allowed_final_roots = (self.root,) + tuple(
            self._validated_allowed_root(item) for item in allowed_final_roots
        )

    @property
    def job_directory(self) -> PurePosixPath:
        return self.root / self._child

    def prepare(self, *, existing_owner: bool, dry_run: bool = False) -> PurePosixPath:
        """Create a new owned directory or accept the exact prior owned directory."""
        if self.filesystem.exists(self.job_directory) and not existing_owner:
            self._storage_error(
                "DOWNLOAD_PATH_COLLISION",
                "The deterministic download job directory already exists without ownership proof.",
            )
        if not dry_run:
            self.filesystem.mkdirs(self.root)
            canonical_root = self.filesystem.canonicalize(self.root)
            self._validate_root(canonical_root)
            self.root = canonical_root
            self.filesystem.mkdirs(self.job_directory)
            info = self.filesystem.lstat(self.job_directory)
            if not info.is_directory or info.is_symlink:
                self._storage_error(
                    "SFTP_PATH_OPERATION_FAILED",
                    "The permanent download directory is not a safe remote directory.",
                )
        return self.job_directory

    def validate_final_path(self, value: str) -> PurePosixPath:
        """Resolve an existing final payload path beneath an approved remote root."""
        try:
            path = self._remote_path(value, "final base path")
            resolved = self.filesystem.canonicalize(path)
            roots = tuple(
                self.filesystem.canonicalize(root) if self.filesystem.exists(root) else root
                for root in self.allowed_final_roots
            )
            if not any(_contains(root, resolved) for root in roots):
                self._post_error(
                    "FINAL_PATH_OUTSIDE_ALLOWED_ROOT",
                    "The final base path is outside configured approved roots.",
                )
            info = self.filesystem.lstat(resolved)
            if info.is_symlink:
                self._post_error("FINAL_PATH_NOT_FOUND", "The final payload path is a symlink.")
            return resolved
        except FileNotFoundError:
            self._post_error("FINAL_PATH_NOT_FOUND", "The final payload path does not exist.")
        except DownloadStorageError as exc:
            self._post_error("FINAL_PATH_NOT_FOUND", str(exc))
        raise AssertionError("unreachable")

    def top_level_paths(
        self,
        base_path: PurePosixPath,
        job_directory: PurePosixPath,
    ) -> list[str]:
        """Return conservative remote payload paths associated with rTorrent's base path."""
        info = self.filesystem.lstat(base_path)
        if base_path == job_directory and info.is_directory:
            return [
                str(item.path) for item in self.filesystem.listdir(base_path) if not item.is_symlink
            ]
        return [str(base_path)]

    def on_disk_size(self, path: PurePosixPath) -> int:
        """Calculate regular-file bytes remotely without following symlinked directories."""
        return self.filesystem.tree_size(path)

    def storage_payload(self) -> dict[str, str]:
        return {"protocol": "sftp", "download_directory": str(self.job_directory)}

    def _validate_root(self, root: PurePosixPath) -> None:
        home = self.filesystem.home()
        if root in {PurePosixPath("/"), home}:
            self._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", "RTORRENT_DOWNLOAD_DIRECTORY is dangerously broad."
            )

    def _validated_allowed_root(self, root: str | PurePosixPath) -> PurePosixPath:
        value = self._remote_path(root, "RTORRENT_ALLOWED_FINAL_ROOTS entry")
        self._validate_root(value)
        return value

    @staticmethod
    def _remote_path(value: str | PurePosixPath, label: str) -> PurePosixPath:
        text = str(value)
        if not text or any(character in text for character in ("\x00", "\n", "\r", "\\")):
            DownloadDirectoryManager._storage_error("UNSAFE_DOWNLOAD_ROOT", f"{label} is unsafe.")
        path = PurePosixPath(text)
        if not path.is_absolute() or any(part == ".." for part in path.parts):
            DownloadDirectoryManager._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", f"{label} must be absolute."
            )
        return path

    @staticmethod
    def _assert_under(path: PurePosixPath, root: PurePosixPath) -> None:
        if not _contains(root, path) or path == root:
            DownloadDirectoryManager._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", "Generated download path escaped its configured root."
            )

    @staticmethod
    def _storage_error(code: str, message: str) -> None:
        error = DownloadStorageError(message)
        error.error_code = code
        raise error

    @staticmethod
    def _post_error(code: str, message: str) -> None:
        error = DownloadPostProcessingError(message)
        error.error_code = code
        raise error


def sanitize_component(value: str, *, maximum: int = 60) -> str:
    """Produce a portable lowercase path component from untrusted display text."""
    normalized = _SAFE_COMPONENT.sub("-", value.casefold()).strip("-")
    return normalized[:maximum].rstrip("-") or "untitled"


def _contains(root: PurePosixPath, path: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
