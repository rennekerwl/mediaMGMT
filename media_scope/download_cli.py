"""CLI for restart-safe rTorrent payload downloading (Step 6)."""

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
from media_scope.download_directories import DownloadDirectoryManager
from media_scope.download_input import load_download_input
from media_scope.download_service import (
    DownloadPolicy,
    TorrentDownloadService,
    make_download_job_id,
)
from media_scope.exceptions import (
    CliInputError,
    DownloadError,
    DownloadInputError,
    DownloadPostProcessingError,
    DownloadStorageError,
    RtorrentError,
)
from media_scope.models import JsonObject
from media_scope.rtorrent_client import RtorrentClient
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.download")
ClientFactory = Callable[..., RtorrentClient]


def build_download_parser() -> argparse.ArgumentParser:
    """Build the separately executable Step 6 parser."""
    parser = JsonArgumentParser(
        prog="media-download-torrent",
        description="Resume and monitor the Step 5-selected torrent to verified completion.",
    )
    parser.add_argument("--health-result", type=Path, help="Step 5 health-result JSON path.")
    parser.add_argument("--output", type=Path, help="Also write the resulting JSON here.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr.")
    parser.add_argument("--poll-interval-seconds", type=_positive_int)
    parser.add_argument("--stall-timeout-seconds", type=_positive_int)
    parser.add_argument("--download-timeout-seconds", type=_nonnegative_int)
    parser.add_argument(
        "--post-completion-policy",
        choices=("stop", "leave-running"),
    )
    parser.add_argument("--resume-stalled", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input, live torrent state, paths, and space without mutations.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run Step 6 and return its documented process exit code."""
    configure_utf8_stdio()
    parser = build_download_parser()
    try:
        args = parser.parse_args(argv)
        if args.health_result is None:
            raise CliInputError("--health-result is required")
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
        health = load_download_input(args.health_result)
        job_id = make_download_job_id(health)
        policy = DownloadPolicy(
            poll_interval_seconds=args.poll_interval_seconds
            or _environment_positive_number("RTORRENT_DOWNLOAD_POLL_INTERVAL_SECONDS", 30),
            stall_timeout_seconds=args.stall_timeout_seconds
            or _environment_positive_number("RTORRENT_STALL_TIMEOUT_SECONDS", 1800),
            overall_timeout_seconds=(
                args.download_timeout_seconds
                if args.download_timeout_seconds is not None
                else _environment_nonnegative_number("RTORRENT_DOWNLOAD_TIMEOUT_SECONDS", 0)
            ),
            minimum_free_space_bytes=_environment_nonnegative_int(
                "RTORRENT_MIN_FREE_SPACE_BYTES", 0
            ),
            post_completion_policy=_post_policy(
                args.post_completion_policy or os.getenv("RTORRENT_POST_COMPLETION_POLICY", "stop")
            ),
            post_processing_grace_seconds=_environment_nonnegative_number(
                "RTORRENT_POST_PROCESS_GRACE_SECONDS", 30
            ),
        )
        root_text = os.getenv("RTORRENT_DOWNLOAD_DIRECTORY", "").strip()
        if not root_text:
            error = DownloadStorageError(
                "RTORRENT_DOWNLOAD_DIRECTORY is missing; configure a dedicated absolute path."
            )
            error.error_code = "UNSAFE_DOWNLOAD_ROOT"
            raise error
        allowed = [
            Path(value.strip())
            for value in os.getenv("RTORRENT_ALLOWED_FINAL_ROOTS", "").split(",")
            if value.strip()
        ]
        probe_text = os.getenv("RTORRENT_PROBE_DIRECTORY", "").strip()
        directories = DownloadDirectoryManager(
            Path(root_text),
            tmdb_id=health.scope.get("tmdb_id", "unknown"),
            title=str(health.scope.get("title") or health.candidate.release_title),
            infohash=health.candidate.infohash,
            probe_root=Path(probe_text) if probe_text else None,
            allowed_final_roots=allowed,
        )
        client = _create_client(client_factory, transport)
        LOGGER.info(
            "Starting download job %s for hash %s against %s.",
            job_id,
            _short_hash(health.candidate.infohash),
            client.sanitized_endpoint,
        )
        with client:
            payload, exit_code = TorrentDownloadService(
                client,
                directories,
                policy,
                job_id=job_id,
            ).run(
                health,
                resume_stalled=args.resume_stalled,
                dry_run=args.dry_run,
            )
    except DownloadInputError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 2
    except (DownloadStorageError, DownloadPostProcessingError) as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        payload["status"] = exc.error_code
        exit_code = exc.exit_code
    except DownloadError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = exc.exit_code
    except RtorrentError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 4
    except Exception:
        LOGGER.exception("Unexpected internal torrent-download failure.")
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


def _error_payload(error_code: str, message: str, *, job_id: str | None = None) -> JsonObject:
    payload: JsonObject = {
        "schema_version": 1,
        "result": "download_failed",
        "error_code": error_code,
        "message": message,
        "status": error_code,
        "ready_for_transfer": False,
        "warnings": [],
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
            LOGGER.exception("Could not write the requested download output file.")
            payload = _error_payload(
                "OUTPUT_WRITE_ERROR",
                "The resulting JSON could not be written to the requested output path.",
                job_id=str(payload.get("job_id", "")) or None,
            )
            text = serialize_json(payload, pretty=pretty)
            exit_code = 9
    sys.stdout.write(text)
    return exit_code


def _post_policy(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in {"stop", "leave_running"}:
        error = DownloadStorageError(
            "RTORRENT_POST_COMPLETION_POLICY must be stop or leave_running."
        )
        error.error_code = "TORRENT_STATE_INVALID"
        error.exit_code = 2
        raise error
    return normalized


def _environment_positive_number(name: str, default: float) -> float:
    parsed = _environment_number(name, default)
    if parsed <= 0:
        error = DownloadStorageError(f"{name} must be positive.")
        error.exit_code = 2
        raise error
    return parsed


def _environment_nonnegative_number(name: str, default: float) -> float:
    parsed = _environment_number(name, default)
    if parsed < 0:
        error = DownloadStorageError(f"{name} must be nonnegative.")
        error.exit_code = 2
        raise error
    return parsed


def _environment_nonnegative_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        error = DownloadStorageError(f"{name} must be a nonnegative integer.")
        error.exit_code = 2
        raise error from exc
    if parsed < 0:
        error = DownloadStorageError(f"{name} must be a nonnegative integer.")
        error.exit_code = 2
        raise error
    return parsed


def _environment_number(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        error = DownloadStorageError(f"{name} must be numeric.")
        error.exit_code = 2
        raise error from exc


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    error = DownloadStorageError(f"{name} must be true or false.")
    error.exit_code = 2
    raise error


def _positive_int(value: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _short_hash(value: str) -> str:
    return f"{value[:8]}...{value[-4:]}"
