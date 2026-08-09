"""CLI for Step 7 movie transfer and seedbox cleanup."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from dotenv import load_dotenv

from media_scope.cli import JsonArgumentParser
from media_scope.exceptions import (
    CliInputError,
    DownloadStorageError,
    RtorrentError,
    SeedboxFilesystemError,
    TransferCleanupError,
    TransferError,
    TransferInputError,
)
from media_scope.models import JsonObject
from media_scope.remote_filesystem import SftpRemoteFilesystem
from media_scope.rtorrent_client import RtorrentClient
from media_scope.serialization import configure_utf8_stdio, serialize_json
from media_scope.transfer_input import load_transfer_input, load_transfer_input_text
from media_scope.transfer_service import MovieTransferService

LOGGER = logging.getLogger("media_scope.transfer")
ClientFactory = Callable[..., RtorrentClient]
FilesystemFactory = Callable[..., SftpRemoteFilesystem]


def build_transfer_parser() -> argparse.ArgumentParser:
    """Build the separately executable Step 7 parser."""
    parser = JsonArgumentParser(
        prog="media-transfer-movie",
        description="Transfer one completed movie locally, then clean it from the seedbox.",
    )
    parser.add_argument(
        "--download-result",
        type=Path,
        help="Read Step 6 JSON from this path instead of standard input.",
    )
    parser.add_argument("--output", type=Path, help="Also write the resulting JSON here.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    input_text: str | None = None,
    client_factory: ClientFactory | None = None,
    filesystem_factory: FilesystemFactory | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run Step 7 and return its documented process exit code."""
    configure_utf8_stdio()
    parser = build_transfer_parser()
    try:
        args = parser.parse_args(argv)
    except CliInputError as exc:
        return _emit(_error_payload(exc.error_code, str(exc)), 2, pretty=False)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv()

    payload: JsonObject
    exit_code = 9
    job_id: str | None = None
    try:
        handoff = (
            load_transfer_input(args.download_result)
            if args.download_result is not None
            else load_transfer_input_text(
                input_text if input_text is not None else sys.stdin.read()
            )
        )
        job_id = handoff.job_id
        movies_text = os.getenv("MOVIES_DIRECTORY", "").strip()
        if not movies_text:
            raise TransferInputError("MOVIES_DIRECTORY is missing.")
        movies_directory = Path(movies_text).expanduser()
        if not movies_directory.is_dir():
            raise TransferInputError("MOVIES_DIRECTORY must identify an accessible directory.")
        download_root = os.getenv("RTORRENT_DOWNLOAD_DIRECTORY", "").strip()
        if not download_root:
            raise TransferInputError("RTORRENT_DOWNLOAD_DIRECTORY is missing.")
        allowed = tuple(
            item.strip()
            for item in os.getenv("RTORRENT_ALLOWED_FINAL_ROOTS", "").split(",")
            if item.strip()
        )
        client = _create_client(client_factory, transport)
        filesystem = _create_filesystem(filesystem_factory)
        LOGGER.info(
            "Starting movie transfer job %s for hash %s.",
            job_id,
            _short_hash(handoff.infohash),
        )
        with client, filesystem:
            payload = MovieTransferService(
                client,
                filesystem,
                movies_directory,
                download_root=download_root,
                allowed_final_roots=allowed,
            ).run(handoff)
        exit_code = 0
    except TransferCleanupError as exc:
        payload = _cleanup_error_payload(exc, job_id=job_id)
        exit_code = exc.exit_code
    except TransferError as exc:
        payload = _error_payload(
            exc.error_code,
            str(exc),
            job_id=job_id,
            local_paths=getattr(exc, "local_paths", None),
        )
        exit_code = exc.exit_code
    except (SeedboxFilesystemError, RtorrentError) as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 4
    except DownloadStorageError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 5
    except Exception:
        LOGGER.exception("Unexpected movie-transfer failure.")
        payload = _error_payload(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred. Enable --verbose for diagnostics.",
            job_id=job_id,
        )
        exit_code = 9
    return _emit(payload, exit_code, pretty=args.pretty, output=args.output)


def _create_client(
    factory: ClientFactory | None,
    transport: httpx.BaseTransport | None,
) -> RtorrentClient:
    constructor = factory or RtorrentClient
    return constructor(
        os.getenv("RTORRENT_RPC_URL", ""),
        username=os.getenv("RTORRENT_RPC_USERNAME", ""),
        password=os.getenv("RTORRENT_RPC_PASSWORD", ""),
        verify_tls=_environment_bool("RTORRENT_RPC_VERIFY_TLS", True),
        timeout_seconds=_environment_positive_number("RTORRENT_RPC_TIMEOUT_SECONDS", 15),
        transport=transport,
    )


def _create_filesystem(factory: FilesystemFactory | None) -> SftpRemoteFilesystem:
    constructor = factory or SftpRemoteFilesystem
    known_hosts_text = os.getenv("SEEDBOX_SSH_KNOWN_HOSTS", "").strip()
    return constructor(
        os.getenv("SEEDBOX_SSH_HOST", "").strip(),
        port=_environment_positive_int("SEEDBOX_SSH_PORT", 22),
        username=os.getenv("SEEDBOX_USERNAME", "").strip(),
        password=os.getenv("SEEDBOX_PASSWORD", ""),
        timeout_seconds=_environment_positive_number("SEEDBOX_SSH_TIMEOUT_SECONDS", 15),
        known_hosts=Path(known_hosts_text) if known_hosts_text else None,
    )


def _error_payload(
    error_code: str,
    message: str,
    *,
    job_id: str | None = None,
    local_paths: list[str] | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "schema_version": 1,
        "result": "transfer_failed",
        "error_code": error_code,
        "message": message,
        "status": error_code,
        "transfer_completed": False,
        "cleanup_performed": False,
        "warnings": [],
    }
    if job_id:
        payload["job_id"] = job_id
    if local_paths:
        payload["partial_local_paths"] = local_paths
    return payload


def _cleanup_error_payload(error: TransferCleanupError, *, job_id: str | None) -> JsonObject:
    cleanup: JsonObject = {
        "status": "FAILED",
        "torrent_removed": getattr(error, "torrent_removed", None),
        "remote_paths_removed": getattr(error, "removed_paths", []),
        "remaining_remote_paths": getattr(error, "remaining_paths", []),
    }
    payload: JsonObject = {
        "schema_version": 1,
        "result": "transfer_completed_cleanup_failed",
        "error_code": error.error_code,
        "message": str(error),
        "transfer": getattr(error, "transfer", {}),
        "cleanup": cleanup,
        "status": "SEEDBOX_CLEANUP_FAILED",
        "warnings": ["The local transfer completed; seedbox cleanup requires manual attention."],
    }
    if job_id:
        payload["job_id"] = job_id
    return payload


def _emit(
    payload: JsonObject,
    exit_code: int,
    *,
    pretty: bool,
    output: Path | None = None,
) -> int:
    text = serialize_json(payload, pretty=pretty)
    if output is not None:
        try:
            output.write_text(text, encoding="utf-8")
        except OSError:
            LOGGER.exception("Could not write the requested transfer output file.")
            payload = _error_payload(
                "OUTPUT_WRITE_ERROR",
                "The transfer result could not be written to the requested output path.",
                job_id=str(payload.get("job_id", "")) or None,
            )
            text = serialize_json(payload, pretty=pretty)
            exit_code = 9
    sys.stdout.write(text)
    return exit_code


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise TransferInputError(f"{name} must be true or false.")


def _environment_positive_number(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise TransferInputError(f"{name} must be numeric.") from exc
    if parsed <= 0:
        raise TransferInputError(f"{name} must be positive.")
    return parsed


def _environment_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise TransferInputError(f"{name} must be a positive integer.") from exc
    if parsed <= 0:
        raise TransferInputError(f"{name} must be a positive integer.")
    return parsed


def _short_hash(value: str) -> str:
    return f"{value[:8]}...{value[-4:]}"
