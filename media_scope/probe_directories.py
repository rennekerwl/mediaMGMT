"""Conservative local directory ownership and cleanup for rTorrent probes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from media_scope.exceptions import RtorrentConfigurationError


class ProbeDirectoryManager:
    """Create and remove only paths owned beneath one configured probe root."""

    def __init__(self, root: Path, job_id: str) -> None:
        if not root.is_absolute():
            raise RtorrentConfigurationError("RTORRENT_PROBE_DIRECTORY must be an absolute path.")
        self.root = root.resolve()
        home = Path.home().resolve()
        anchor = Path(self.root.anchor).resolve()
        if self.root in {anchor, home} or self.root in home.parents:
            raise RtorrentConfigurationError(
                "RTORRENT_PROBE_DIRECTORY is dangerously broad; choose a dedicated directory."
            )
        if not job_id.startswith("probe-") or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in job_id
        ):
            raise RtorrentConfigurationError("Generated probe job ID is not path-safe.")
        self.job_directory = (self.root / job_id).resolve()
        self._assert_owned(self.job_directory)

    def prepare_job(self) -> None:
        """Create the root and unique job directory with owner-only permissions."""
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.job_directory.mkdir(mode=0o700)
        except OSError as exc:
            raise RtorrentConfigurationError(
                "The dedicated rTorrent probe job directory could not be created."
            ) from exc
        try:
            os.chmod(self.job_directory, 0o700)
        except OSError:
            pass

    def prepare_candidate(self, label: str) -> Path:
        """Create one injection-safe child directory for an owned candidate."""
        if not label or any(character not in "0123456789abcdefABCDEF-_" for character in label):
            raise RtorrentConfigurationError("Probe child-directory label is unsafe.")
        path = (self.job_directory / label).resolve()
        self._assert_owned(path)
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise RtorrentConfigurationError(
                "The dedicated rTorrent candidate directory could not be created."
            ) from exc
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    def cleanup_candidate(self, path: Path) -> None:
        """Remove exactly one verified owned candidate directory, if present."""
        resolved = path.resolve()
        self._assert_owned(resolved)
        if resolved == self.job_directory:
            raise RtorrentConfigurationError(
                "Refusing candidate cleanup of the whole job directory."
            )
        if resolved.exists():
            shutil.rmtree(resolved)

    def cleanup_empty_job(self) -> None:
        """Remove the unique job directory only when it is empty."""
        self._assert_owned(self.job_directory)
        try:
            self.job_directory.rmdir()
        except OSError:
            pass

    def _assert_owned(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RtorrentConfigurationError(
                "Refusing filesystem operation outside RTORRENT_PROBE_DIRECTORY."
            ) from exc
        if path == self.root:
            raise RtorrentConfigurationError("Refusing to operate on the shared probe root.")
