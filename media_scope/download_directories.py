"""Safe permanent-directory and final-path handling for Step 6."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from media_scope.exceptions import DownloadPostProcessingError, DownloadStorageError

DiskUsage = Callable[[Path], shutil._ntuple_diskusage]
_SAFE_COMPONENT = re.compile(r"[^a-z0-9]+")


class DownloadDirectoryManager:
    """Validate and prepare a deterministic server-side download directory."""

    def __init__(
        self,
        root: Path,
        *,
        tmdb_id: int | str,
        title: str,
        infohash: str,
        probe_root: Path | None = None,
        allowed_final_roots: Iterable[Path] = (),
        disk_usage: DiskUsage = shutil.disk_usage,
    ) -> None:
        if not root.is_absolute():
            self._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", "RTORRENT_DOWNLOAD_DIRECTORY must be absolute."
            )
        self.root = root.resolve()
        self._validate_root(self.root)
        self.probe_root = probe_root.resolve() if probe_root is not None else None
        if self.probe_root is not None:
            resolved_probe = self.probe_root
            if _contains(self.root, resolved_probe) or _contains(resolved_probe, self.root):
                self._storage_error(
                    "UNSAFE_DOWNLOAD_ROOT",
                    "Probe and permanent download directories must not overlap.",
                )
        slug = sanitize_component(title)
        tmdb = sanitize_component(str(tmdb_id)) or "unknown"
        child = f"{tmdb}-{slug}-{infohash[:8].lower()}"
        self.job_directory = (self.root / child).resolve()
        self._assert_under(self.job_directory, self.root, storage=True)
        self.allowed_final_roots = (self.root,) + tuple(
            self._validated_allowed_root(item) for item in allowed_final_roots
        )
        self.disk_usage = disk_usage

    def prepare(self, *, existing_owner: bool, dry_run: bool = False) -> Path:
        """Create a new owned directory or accept the exact prior owned directory."""
        if self.job_directory.exists() and not existing_owner:
            self._storage_error(
                "DOWNLOAD_PATH_COLLISION",
                "The deterministic download job directory already exists without ownership proof.",
            )
        if not dry_run:
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.job_directory.mkdir(mode=0o700, exist_ok=existing_owner)
            except OSError as exc:
                raise DownloadStorageError(
                    "The permanent download directory could not be created."
                ) from exc
            if not os.access(self.job_directory, os.W_OK):
                raise DownloadStorageError(
                    "The permanent download directory is not writable by this process."
                )
        return self.job_directory

    def free_bytes(self) -> int:
        """Return free bytes on the closest existing ancestor's filesystem."""
        target = self.job_directory
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            return int(self.disk_usage(target).free)
        except OSError as exc:
            raise DownloadStorageError("Available disk space could not be determined.") from exc

    def validate_final_path(self, value: str) -> Path:
        """Resolve an existing final payload path beneath an approved root."""
        if not value or any(character in value for character in ("\x00", "\n", "\r")):
            self._post_error("FINAL_PATH_NOT_FOUND", "rTorrent returned no safe final base path.")
        path = Path(value)
        if not path.is_absolute():
            self._post_error("FINAL_PATH_NOT_FOUND", "The final base path is not absolute.")
        resolved = path.resolve()
        if not any(_contains(root, resolved) for root in self.allowed_final_roots):
            self._post_error(
                "FINAL_PATH_OUTSIDE_ALLOWED_ROOT",
                "The final base path is outside configured approved roots.",
            )
        if not resolved.exists():
            self._post_error("FINAL_PATH_NOT_FOUND", "The final payload path does not exist.")
        return resolved

    @staticmethod
    def top_level_paths(base_path: Path, job_directory: Path) -> list[str]:
        """Return conservative payload paths associated with rTorrent's base path."""
        if base_path == job_directory and base_path.is_dir():
            return [
                str(item.resolve()) for item in sorted(base_path.iterdir(), key=lambda p: p.name)
            ]
        return [str(base_path)]

    @staticmethod
    def on_disk_size(path: Path) -> int:
        """Calculate regular-file bytes without following symlinked directories."""
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, directories, files in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
            for name in files:
                item = Path(root) / name
                if not item.is_symlink():
                    try:
                        total += item.stat().st_size
                    except OSError:
                        continue
        return total

    @staticmethod
    def _validate_root(root: Path) -> None:
        home = Path.home().resolve()
        anchor = Path(root.anchor).resolve()
        if root in {anchor, home} or root in home.parents:
            DownloadDirectoryManager._storage_error(
                "UNSAFE_DOWNLOAD_ROOT",
                "RTORRENT_DOWNLOAD_DIRECTORY is dangerously broad.",
            )

    def _validated_allowed_root(self, root: Path) -> Path:
        if not root.is_absolute():
            self._storage_error(
                "UNSAFE_DOWNLOAD_ROOT", "Every RTORRENT_ALLOWED_FINAL_ROOTS entry must be absolute."
            )
        resolved = root.resolve()
        self._validate_root(resolved)
        return resolved

    @staticmethod
    def _assert_under(path: Path, root: Path, *, storage: bool) -> None:
        if not _contains(root, path) or path == root:
            if storage:
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


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
