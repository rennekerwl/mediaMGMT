"""CLI for deterministic live rTorrent torrent-health validation."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from media_scope.cli import JsonArgumentParser
from media_scope.exceptions import (
    CliInputError,
    NoProbeCandidatesError,
    ProbeInputError,
    RtorrentError,
)
from media_scope.models import JsonObject
from media_scope.probe_directories import ProbeDirectoryManager
from media_scope.probe_input import load_probe_input
from media_scope.probe_service import ProbePolicy, TorrentProbeService
from media_scope.rtorrent_client import RtorrentClient
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.probe")
ClientFactory = Callable[..., RtorrentClient]


def build_probe_parser() -> argparse.ArgumentParser:
    """Build the public probe and connection-diagnostic parser."""
    parser = JsonArgumentParser(
        prog="media-probe-torrents",
        description="Select the first ranked magnet whose metadata rTorrent can retrieve.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="probe",
        choices=("probe", "check-connection"),
    )
    parser.add_argument("--search-results", type=Path, help="Jackett search-results JSON path.")
    parser.add_argument("--output", type=Path, help="Also write the resulting JSON here.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr.")
    parser.add_argument("--max-candidates", type=_positive_int)
    parser.add_argument("--timeout-seconds", type=_positive_int)
    parser.add_argument("--poll-interval-seconds", type=_positive_int)
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument(
        "--keep-failed-probes",
        action="store_true",
        help="DANGEROUS: retain failed probe torrents and their probe data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and show probe order without calling rTorrent.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run the probe CLI and return its documented process exit code."""
    configure_utf8_stdio()
    parser = build_probe_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "probe" and args.search_results is None:
            raise CliInputError("--search-results is required for normal probing")
        if args.command == "check-connection" and args.dry_run:
            raise CliInputError("--dry-run cannot be combined with check-connection")
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

    job_id = _new_job_id()
    payload: JsonObject
    exit_code = 8
    try:
        if args.command == "probe":
            search_input = load_probe_input(args.search_results)
            maximum_candidates = args.max_candidates or _environment_positive_int(
                "RTORRENT_PROBE_MAX_CANDIDATES", 10
            )
            timeout = args.timeout_seconds or _environment_positive_int(
                "RTORRENT_METADATA_TIMEOUT_SECONDS", 300
            )
            poll_interval = args.poll_interval_seconds or _environment_positive_int(
                "RTORRENT_METADATA_POLL_INTERVAL_SECONDS", 5
            )
            if args.dry_run:
                payload = _dry_run_payload(
                    job_id,
                    search_input.scope,
                    search_input.candidates,
                    maximum_candidates,
                    timeout,
                    poll_interval,
                )
                exit_code = 0
            else:
                preflight_timeout = _environment_positive_int(
                    "RTORRENT_PREFLIGHT_TIMEOUT_SECONDS", 120
                )
                policy = ProbePolicy(
                    maximum_candidates=maximum_candidates,
                    metadata_timeout_seconds=timeout,
                    poll_interval_seconds=poll_interval,
                    preflight_timeout_seconds=preflight_timeout,
                    keep_failed_probes=args.keep_failed_probes,
                )
                client = _create_client(client_factory, transport)
                LOGGER.info("Starting probe job %s against %s.", job_id, client.sanitized_endpoint)
                with client:
                    directories = _create_directories(client, job_id)
                    payload, exit_code = TorrentProbeService(
                        client,
                        directories,
                        policy,
                        job_id=job_id,
                    ).run(
                        search_input,
                        preflight_magnet=os.getenv("RTORRENT_PREFLIGHT_MAGNET", "").strip() or None,
                        skip_preflight=args.skip_preflight,
                    )
        else:
            client = _create_client(client_factory, transport)
            with client:
                capabilities = client.discover_capabilities()
                directories = _create_directories(client, job_id)
                directories.check_root()
            payload = {
                "schema_version": 1,
                "result": "connection_ok",
                "rtorrent": {
                    "client_version": capabilities.client_version,
                    "library_version": capabilities.library_version,
                    "api_version": capabilities.api_version,
                    "rpc_endpoint": client.sanitized_endpoint,
                    "load_method": capabilities.load_method,
                    "metadata_detection_method": capabilities.metadata_detection_method,
                    "available_required_methods": sorted(capabilities.methods),
                },
                "probe_directory_ready": True,
                "torrent_added": False,
                "warnings": [],
            }
            exit_code = 0
    except NoProbeCandidatesError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 3
    except ProbeInputError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 2
    except RtorrentError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 4
    except Exception:
        LOGGER.exception("Unexpected internal torrent-probe failure.")
        payload = _error_payload(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred. Enable --verbose for diagnostics.",
            job_id=job_id,
        )
        exit_code = 8

    return _emit(payload, exit_code, pretty=args.pretty, output=args.output)


def _create_client(
    factory: ClientFactory | None,
    transport: httpx.BaseTransport | None,
) -> RtorrentClient:
    verify_tls = _environment_bool("RTORRENT_RPC_VERIFY_TLS", True)
    timeout = _environment_positive_float("RTORRENT_RPC_TIMEOUT_SECONDS", 15)
    constructor = factory or RtorrentClient
    return constructor(
        os.getenv("RTORRENT_RPC_URL", ""),
        username=os.getenv("RTORRENT_RPC_USERNAME", ""),
        password=os.getenv("RTORRENT_RPC_PASSWORD", ""),
        verify_tls=verify_tls,
        timeout_seconds=timeout,
        transport=transport,
    )


def _create_directories(client: RtorrentClient, job_id: str) -> ProbeDirectoryManager:
    root = os.getenv("RTORRENT_PROBE_DIRECTORY", "").strip()
    if not root:
        from media_scope.exceptions import RtorrentConfigurationError

        raise RtorrentConfigurationError(
            "RTORRENT_PROBE_DIRECTORY is missing; configure a dedicated seedbox path."
        )
    return ProbeDirectoryManager(client, root, job_id)


def _dry_run_payload(
    job_id: str,
    scope: JsonObject,
    candidates: tuple[Any, ...],
    maximum: int,
    timeout: int,
    interval: int,
) -> JsonObject:
    planned = candidates[:maximum]
    return {
        "schema_version": 1,
        "result": "dry_run",
        "job_id": job_id,
        "scope": scope,
        "policy": {
            "metadata_timeout_seconds": timeout,
            "poll_interval_seconds": interval,
            "maximum_candidates": maximum,
            "content_validation_performed": False,
            "stop_after_first_healthy": True,
        },
        "planned_probe_order": [
            {
                "original_rank": item.rank,
                "original_score": item.score,
                "infohash": item.infohash,
                "release_title": item.release_title,
            }
            for item in planned
        ],
        "unattempted_candidates": [
            {
                "original_rank": item.rank,
                "infohash": item.infohash,
                "reason": "CANDIDATE_LIMIT",
            }
            for item in candidates[maximum:]
        ],
        "rtorrent_called": False,
        "selected_candidate": None,
        "warnings": ["Dry-run mode does not validate live torrent health."],
    }


def _error_payload(error_code: str, message: str, *, job_id: str | None = None) -> JsonObject:
    payload: JsonObject = {
        "schema_version": 1,
        "result": "error",
        "error_code": error_code,
        "message": message,
        "selected_candidate": None,
        "warnings": [],
    }
    if job_id is not None:
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
            LOGGER.exception("Could not write the requested probe output file.")
            payload = _error_payload(
                "OUTPUT_WRITE_ERROR",
                "The resulting JSON could not be written to the requested output path.",
                job_id=str(payload.get("job_id", "")) or None,
            )
            text = serialize_json(payload, pretty=pretty)
            exit_code = 8
    sys.stdout.write(text)
    return exit_code


def _new_job_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"probe-{stamp}-{uuid.uuid4().hex[:8]}"


def _environment_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        from media_scope.exceptions import RtorrentConfigurationError

        raise RtorrentConfigurationError(f"{name} must be a positive integer.") from exc
    if parsed <= 0:
        from media_scope.exceptions import RtorrentConfigurationError

        raise RtorrentConfigurationError(f"{name} must be a positive integer.")
    return parsed


def _environment_positive_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        from media_scope.exceptions import RtorrentConfigurationError

        raise RtorrentConfigurationError(f"{name} must be positive.") from exc
    if parsed <= 0:
        from media_scope.exceptions import RtorrentConfigurationError

        raise RtorrentConfigurationError(f"{name} must be positive.")
    return parsed


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    from media_scope.exceptions import RtorrentConfigurationError

    raise RtorrentConfigurationError(f"{name} must be true or false.")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed
