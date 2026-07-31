"""Command-line interface for deterministic Jackett television searches."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from dotenv import load_dotenv

from media_scope.cli import JsonArgumentParser
from media_scope.exceptions import (
    AllIndexersFailedError,
    CliInputError,
    JackettConfigurationError,
    JackettError,
    SearchInputError,
    UnsupportedSearchScopeError,
)
from media_scope.jackett_client import JackettClient
from media_scope.models import JsonObject
from media_scope.scope_input import load_search_scope
from media_scope.search_service import search_complete_series
from media_scope.serialization import serialize_json

LOGGER = logging.getLogger("media_scope.search")
ClientFactory = Callable[[str, str], JackettClient]


def build_search_parser() -> argparse.ArgumentParser:
    """Build the public complete-series search parser."""
    parser = JsonArgumentParser(
        prog="media-search-tv",
        description="Search Jackett for likely complete-series television releases.",
    )
    parser.add_argument("--scope", required=True, type=Path, help="Completed-TV scope JSON path.")
    parser.add_argument("--output", type=Path, help="Also write the resulting JSON here.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr.")
    parser.add_argument(
        "--indexer",
        action="append",
        default=[],
        metavar="INDEXER_ID",
        help="Restrict search to this Jackett indexer; may be repeated.",
    )
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Include complete normalized details for rejected results.",
    )
    parser.add_argument(
        "--max-rejected",
        type=_nonnegative_int,
        default=25,
        help="Maximum rejected-result details to serialize (default: 25).",
    )
    parser.add_argument(
        "--min-seeders",
        type=_nonnegative_int,
        help="Override the ranking-only seeder threshold.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ask Jackett to bypass its Torznab result cache.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
) -> int:
    """Run the search CLI and return its documented process exit code."""
    parser = build_search_parser()
    try:
        args = parser.parse_args(argv)
        indexer_values = _deduplicate_indexers(args.indexer)
    except CliInputError as exc:
        sys.stdout.write(serialize_json(_error_payload(exc.error_code, str(exc))))
        return 2

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv()

    exit_code = 5
    try:
        scope = load_search_scope(args.scope)
        if not indexer_values:
            indexer_values = _deduplicate_indexers(os.getenv("JACKETT_INDEXERS", "").split(","))
        min_seeders = (
            args.min_seeders
            if args.min_seeders is not None
            else _environment_nonnegative_int("MEDIA_SEARCH_MIN_SEEDERS", default=0)
        )
        base_url = os.getenv("JACKETT_URL", "")
        api_key = os.getenv("JACKETT_API_KEY", "")
        factory = client_factory or JackettClient
        with factory(base_url, api_key) as client:
            payload = search_complete_series(
                client,
                scope,
                indexer_ids=indexer_values,
                fresh=args.fresh,
                min_seeders=min_seeders,
                include_rejected=args.include_rejected,
                max_rejected=args.max_rejected,
            )
        exit_code = 0
    except SearchInputError as exc:
        payload = _error_payload(exc.error_code, str(exc))
        exit_code = 2
    except UnsupportedSearchScopeError as exc:
        payload = _error_payload(exc.error_code, str(exc))
        exit_code = 3
    except AllIndexersFailedError as exc:
        payload = _error_payload(exc.error_code, str(exc))
        payload["indexer_diagnostics"] = exc.diagnostics
        exit_code = 4
    except (JackettConfigurationError, JackettError) as exc:
        payload = _error_payload(exc.error_code, str(exc))
        exit_code = 4
    except Exception:
        LOGGER.exception("Unexpected internal search failure.")
        payload = _error_payload(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred. Enable --verbose for diagnostics.",
        )
        exit_code = 5

    text = serialize_json(payload, pretty=args.pretty)
    if args.output is not None:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError:
            LOGGER.exception("Could not write the requested search output file.")
            payload = _error_payload(
                "OUTPUT_WRITE_ERROR",
                "The resulting JSON could not be written to the requested output path.",
            )
            text = serialize_json(payload, pretty=args.pretty)
            exit_code = 5
    sys.stdout.write(text)
    return exit_code


def _error_payload(error_code: str, message: str) -> JsonObject:
    return {
        "schema_version": 1,
        "result": "error",
        "media_type": "tv",
        "error_code": error_code,
        "message": message,
        "warnings": [],
    }


def _deduplicate_indexers(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        key = stripped.casefold()
        if key not in seen:
            seen.add(key)
            result.append(stripped)
    return tuple(result)


def _environment_nonnegative_int(name: str, *, default: int) -> int:
    value = os.getenv(name, "")
    if not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise JackettConfigurationError(f"{name} must be a nonnegative integer.") from exc
    if parsed < 0:
        raise JackettConfigurationError(f"{name} must be a nonnegative integer.")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed
