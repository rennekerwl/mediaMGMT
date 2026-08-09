"""Direct SFTP movie transfer followed by exact seedbox cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from media_scope.download_models import DownloadSnapshot
from media_scope.exceptions import (
    DownloadStorageError,
    RtorrentError,
    RtorrentMethodError,
    SeedboxFilesystemError,
    TransferCleanupError,
    TransferStorageError,
    TransferTorrentError,
)
from media_scope.models import JsonObject
from media_scope.remote_filesystem import RemoteFilesystem
from media_scope.rtorrent_client import RtorrentClient
from media_scope.transfer_input import MovieTransferInput


@dataclass(frozen=True, slots=True)
class TransferFile:
    """One preflighted remote file and its exact local destination."""

    source: PurePosixPath
    destination: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class TransferManifest:
    """Complete no-collision transfer manifest."""

    sources: tuple[PurePosixPath, ...]
    local_paths: tuple[Path, ...]
    directories: tuple[Path, ...]
    files: tuple[TransferFile, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


class MovieTransferService:
    """Validate, transfer, and clean up one completed movie job."""

    def __init__(
        self,
        client: RtorrentClient,
        filesystem: RemoteFilesystem,
        movies_directory: Path,
        *,
        download_root: str | PurePosixPath,
        allowed_final_roots: tuple[str | PurePosixPath, ...] = (),
    ) -> None:
        self.client = client
        self.filesystem = filesystem
        self.movies_directory = movies_directory.resolve()
        self.download_root = self._configured_root(download_root, "RTORRENT_DOWNLOAD_DIRECTORY")
        self.allowed_roots = (self.download_root,) + tuple(
            self._configured_root(value, "RTORRENT_ALLOWED_FINAL_ROOTS entry")
            for value in allowed_final_roots
        )

    def run(self, handoff: MovieTransferInput) -> JsonObject:
        """Transfer the entire payload, then remove its torrent and remote files."""
        snapshot = self._validate_torrent(handoff)
        manifest = self._build_manifest(handoff, snapshot)
        transfer = self._transfer(manifest)
        cleanup = self._cleanup(handoff, manifest, transfer)
        return {
            "schema_version": 1,
            "result": "transfer_completed",
            "job_id": handoff.job_id,
            "scope": handoff.scope,
            "candidate": handoff.candidate,
            "transfer": transfer,
            "cleanup": cleanup,
            "status": "TRANSFER_AND_CLEANUP_COMPLETED",
            "warnings": [],
        }

    def _validate_torrent(self, handoff: MovieTransferInput) -> DownloadSnapshot:
        capabilities = self.client.discover_download_capabilities()
        if "d.erase" not in capabilities.methods:
            raise RtorrentMethodError(
                "Connected rTorrent does not expose the required d.erase cleanup operation."
            )
        if not self.client.torrent_exists(handoff.infohash):
            error = TransferTorrentError("The Step 6 torrent no longer exists in rTorrent.")
            error.error_code = "SELECTED_TORRENT_NOT_FOUND"
            raise error
        owner = self.client.get_custom(handoff.infohash, "media_download_job_id")
        if owner != handoff.job_id:
            error = TransferTorrentError("The retained torrent is owned by a different media job.")
            error.error_code = "TORRENT_IDENTITY_MISMATCH"
            raise error
        state = self.client.get_custom(handoff.infohash, "media_download_state")
        if state != "READY_FOR_TRANSFER":
            raise TransferTorrentError("The retained torrent is no longer READY_FOR_TRANSFER.")
        snapshot = self.client.download_snapshot(handoff.infohash)
        if snapshot.infohash.casefold() != handoff.infohash.casefold():
            error = TransferTorrentError("rTorrent returned a different torrent identity.")
            error.error_code = "TORRENT_IDENTITY_MISMATCH"
            raise error
        if not _is_complete(snapshot):
            raise TransferTorrentError(
                "The retained torrent no longer has a verified completion state."
            )
        return snapshot

    def _build_manifest(
        self,
        handoff: MovieTransferInput,
        snapshot: DownloadSnapshot,
    ) -> TransferManifest:
        try:
            roots = tuple(self._canonical_root(root) for root in self.allowed_roots)
            remote_home = self.filesystem.canonicalize(self.filesystem.home())
            if any(root == remote_home for root in roots):
                self._storage_error(
                    "UNSAFE_REMOTE_ROOT",
                    "Configured remote roots cannot be the seedbox home directory.",
                )
            canonical_download_root = roots[0]
            download_directory = (
                self._canonical_existing(handoff.download_directory)
                if self.filesystem.exists(handoff.download_directory)
                else handoff.download_directory
            )
            if not _is_strictly_under(download_directory, canonical_download_root):
                self._storage_error(
                    "UNSAFE_REMOTE_PATH",
                    "The Step 6 download directory is outside RTORRENT_DOWNLOAD_DIRECTORY.",
                )

            final_path = self._canonical_existing(handoff.final_base_path)
            live_base_path = self._canonical_existing(_remote_path(snapshot.base_path))
            if live_base_path != final_path:
                self._storage_error(
                    "REMOTE_PATH_MISMATCH",
                    "rTorrent's current base path differs from the Step 6 handoff.",
                )

            canonical_sources: list[PurePosixPath] = []
            for source in handoff.top_level_paths:
                info = self.filesystem.lstat(source)
                if info.is_symlink:
                    self._storage_error("UNSAFE_REMOTE_PATH", "A transfer source is a symlink.")
                canonical = self._canonical_existing(source)
                if canonical != source:
                    self._storage_error(
                        "UNSAFE_REMOTE_PATH",
                        "A transfer source resolves through a different remote path.",
                    )
                if not any(_is_strictly_under(canonical, root) for root in roots):
                    self._storage_error(
                        "UNSAFE_REMOTE_PATH",
                        "A transfer source is outside the configured remote roots.",
                    )
                canonical_sources.append(canonical)

            final_info = self.filesystem.lstat(final_path)
            if final_path == download_directory and final_info.is_directory:
                expected = tuple(
                    item.path for item in self.filesystem.listdir(final_path) if not item.is_symlink
                )
            else:
                expected = (final_path,)
            if set(canonical_sources) != set(expected):
                self._storage_error(
                    "REMOTE_PATH_MISMATCH",
                    "The transfer paths no longer match rTorrent's completed base path.",
                )

            local_paths = tuple(self._local_top_level(source) for source in canonical_sources)
            if len(set(local_paths)) != len(local_paths):
                self._storage_error(
                    "DESTINATION_COLLISION",
                    "Multiple remote sources map to the same local destination.",
                )
            collisions = [path for path in local_paths if path.exists()]
            if collisions:
                error = TransferStorageError(
                    "A top-level destination already exists in MOVIES_DIRECTORY."
                )
                error.error_code = "DESTINATION_COLLISION"
                error.local_paths = [str(path) for path in collisions]  # type: ignore[attr-defined]
                raise error

            directories: list[Path] = []
            files: list[TransferFile] = []
            for source, destination in zip(canonical_sources, local_paths, strict=True):
                self._scan(source, destination, directories, files)
            all_destinations = directories + [item.destination for item in files]
            destination_keys = {str(path).casefold() for path in all_destinations}
            if len(destination_keys) != len(all_destinations):
                self._storage_error(
                    "DESTINATION_COLLISION",
                    "Remote payload names collide on the local filesystem.",
                )
            existing = [path for path in all_destinations if path.exists()]
            if existing:
                error = TransferStorageError(
                    "A payload destination already exists in MOVIES_DIRECTORY."
                )
                error.error_code = "DESTINATION_COLLISION"
                error.local_paths = [str(path) for path in existing]  # type: ignore[attr-defined]
                raise error
            return TransferManifest(
                sources=tuple(canonical_sources),
                local_paths=local_paths,
                directories=tuple(directories),
                files=tuple(files),
            )
        except TransferStorageError:
            raise
        except (DownloadStorageError, OSError, ValueError) as exc:
            error = TransferStorageError("The remote transfer paths could not be validated safely.")
            error.error_code = "REMOTE_PATH_VALIDATION_FAILED"
            raise error from exc

    def _scan(
        self,
        source: PurePosixPath,
        destination: Path,
        directories: list[Path],
        files: list[TransferFile],
    ) -> None:
        self._assert_local_destination(destination)
        info = self.filesystem.lstat(source)
        if info.is_symlink:
            self._storage_error("UNSAFE_REMOTE_PATH", "The remote payload contains a symlink.")
        if info.is_file:
            files.append(TransferFile(source, destination, info.size_bytes))
            return
        if not info.is_directory:
            self._storage_error(
                "UNSUPPORTED_REMOTE_ENTRY",
                "The remote payload contains an unsupported filesystem entry.",
            )
        directories.append(destination)
        for child in self.filesystem.listdir(source):
            if child.path.parent != source or child.path.name in {"", ".", ".."}:
                self._storage_error("UNSAFE_REMOTE_PATH", "SFTP returned an unsafe child path.")
            self._scan(child.path, destination / child.path.name, directories, files)

    def _transfer(self, manifest: TransferManifest) -> JsonObject:
        try:
            for directory in manifest.directories:
                directory.mkdir()
            for item in manifest.files:
                self.filesystem.download_file(item.source, item.destination)
        except SeedboxFilesystemError as exc:
            error = TransferStorageError(
                "The SFTP connection failed during transfer; seedbox cleanup was not started."
            )
            error.error_code = exc.error_code
            error.exit_code = 4
            error.local_paths = [str(path) for path in manifest.local_paths if path.exists()]  # type: ignore[attr-defined]
            raise error from exc
        except (DownloadStorageError, OSError) as exc:
            error = TransferStorageError(
                "The SFTP transfer did not complete; seedbox cleanup was not started."
            )
            error.error_code = "SFTP_TRANSFER_FAILED"
            error.local_paths = [str(path) for path in manifest.local_paths if path.exists()]  # type: ignore[attr-defined]
            raise error from exc
        return {
            "status": "COMPLETED",
            "destination_root": str(self.movies_directory),
            "local_paths": [str(path) for path in manifest.local_paths],
            "file_count": len(manifest.files),
            "bytes_transferred": manifest.total_size_bytes,
        }

    def _cleanup(
        self,
        handoff: MovieTransferInput,
        manifest: TransferManifest,
        transfer: JsonObject,
    ) -> JsonObject:
        torrent_removed: bool | None = False
        removed_paths: list[str] = []
        try:
            self.client.stop(handoff.infohash)
            self.client.close_download(handoff.infohash)
            self.client.erase(handoff.infohash)
            torrent_removed = not self.client.torrent_exists(handoff.infohash)
            if not torrent_removed:
                raise TransferCleanupError("rTorrent still contains the torrent after d.erase.")
        except TransferCleanupError as exc:
            self._attach_cleanup_context(
                exc,
                transfer,
                torrent_removed=torrent_removed,
                remaining_paths=manifest.sources,
            )
            raise
        except RtorrentError as exc:
            error = TransferCleanupError(
                "The local transfer completed, but rTorrent cleanup failed."
            )
            error.error_code = exc.error_code
            self._attach_cleanup_context(
                error,
                transfer,
                torrent_removed=None,
                remaining_paths=manifest.sources,
            )
            raise error from exc
        except Exception as exc:
            error = TransferCleanupError(
                "The local transfer completed, but rTorrent cleanup failed."
            )
            self._attach_cleanup_context(
                error,
                transfer,
                torrent_removed=None,
                remaining_paths=manifest.sources,
            )
            raise error from exc

        try:
            for source in manifest.sources:
                self.filesystem.remove_tree(source)
                removed_paths.append(str(source))
            directory_removed = self._remove_empty_download_directory(handoff.download_directory)
        except (DownloadStorageError, SeedboxFilesystemError, OSError, FileNotFoundError) as exc:
            remaining = self._remaining_paths(manifest.sources)
            error = TransferCleanupError(
                "The local transfer and torrent removal completed, but remote file cleanup failed."
            )
            self._attach_cleanup_context(
                error,
                transfer,
                torrent_removed=True,
                remaining_paths=remaining,
                removed_paths=removed_paths,
            )
            raise error from exc
        return {
            "status": "COMPLETED",
            "torrent_removed": True,
            "remote_paths_removed": removed_paths,
            "download_directory_removed": directory_removed,
        }

    def _remove_empty_download_directory(self, path: PurePosixPath) -> bool:
        if not self.filesystem.exists(path):
            return False
        info = self.filesystem.lstat(path)
        if info.is_symlink or not info.is_directory or self.filesystem.listdir(path):
            return False
        self.filesystem.remove_tree(path)
        return True

    def _remaining_paths(self, sources: tuple[PurePosixPath, ...]) -> tuple[PurePosixPath, ...]:
        remaining: list[PurePosixPath] = []
        for source in sources:
            try:
                if self.filesystem.exists(source):
                    remaining.append(source)
            except Exception:
                remaining.append(source)
        return tuple(remaining)

    @staticmethod
    def _attach_cleanup_context(
        error: TransferCleanupError,
        transfer: JsonObject,
        *,
        torrent_removed: bool | None,
        remaining_paths: tuple[PurePosixPath, ...],
        removed_paths: list[str] | None = None,
    ) -> None:
        error.transfer = transfer  # type: ignore[attr-defined]
        error.torrent_removed = torrent_removed  # type: ignore[attr-defined]
        error.remaining_paths = [str(path) for path in remaining_paths]  # type: ignore[attr-defined]
        error.removed_paths = list(removed_paths or [])  # type: ignore[attr-defined]

    def _canonical_root(self, path: PurePosixPath) -> PurePosixPath:
        return self.filesystem.canonicalize(path) if self.filesystem.exists(path) else path

    def _canonical_existing(self, path: PurePosixPath) -> PurePosixPath:
        info = self.filesystem.lstat(path)
        if info.is_symlink:
            self._storage_error("UNSAFE_REMOTE_PATH", "A required remote path is a symlink.")
        return self.filesystem.canonicalize(path)

    def _local_top_level(self, source: PurePosixPath) -> Path:
        destination = self.movies_directory / source.name
        self._assert_local_destination(destination)
        return destination

    def _assert_local_destination(self, destination: Path) -> None:
        resolved = destination.resolve()
        try:
            resolved.relative_to(self.movies_directory)
        except ValueError:
            self._storage_error(
                "UNSAFE_LOCAL_PATH", "A remote name resolves outside MOVIES_DIRECTORY."
            )
        if resolved == self.movies_directory:
            self._storage_error("UNSAFE_LOCAL_PATH", "A transfer cannot replace MOVIES_DIRECTORY.")

    @staticmethod
    def _configured_root(value: str | PurePosixPath, field: str) -> PurePosixPath:
        try:
            path = _remote_path(value)
        except ValueError as exc:
            error = TransferStorageError(f"{field} must be a safe absolute POSIX path.")
            error.error_code = "UNSAFE_REMOTE_ROOT"
            raise error from exc
        if path == PurePosixPath("/"):
            error = TransferStorageError(f"{field} cannot be the remote filesystem root.")
            error.error_code = "UNSAFE_REMOTE_ROOT"
            raise error
        return path

    @staticmethod
    def _storage_error(code: str, message: str) -> None:
        error = TransferStorageError(message)
        error.error_code = code
        raise error


def _remote_path(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Remote paths must be absolute and cannot contain traversal.")
    return path


def _is_strictly_under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path != root and root in path.parents


def _is_complete(snapshot: DownloadSnapshot) -> bool:
    message = (snapshot.message or "").casefold()
    terminal_markers = (
        "permission denied",
        "no space left",
        "disk full",
        "input/output error",
        "hash check failed",
        "could not open file",
        "failed to save",
    )
    return (
        snapshot.complete
        and snapshot.size_bytes > 0
        and snapshot.completed_bytes >= snapshot.size_bytes
        and snapshot.left_bytes == 0
        and snapshot.download_rate == 0
        and not snapshot.hashing
        and not any(marker in message for marker in terminal_markers)
    )
