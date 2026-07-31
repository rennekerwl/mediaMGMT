"""Command-line interface for deterministic media-scope resolution."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn, cast

from dotenv import load_dotenv

from media_scope.client import TmdbClient
from media_scope.exceptions import (
    AmbiguityError,
    AuthenticationError,
    CliInputError,
    TmdbError,
)
from media_scope.models import JsonObject
from media_scope.resolver import MediaType, resolve_media
from media_scope.scope_builder import build_movie_scope, build_tv_scope
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope")
ClientFactory = Callable[[str], TmdbClient]


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument parser that raises errors for structured JSON handling."""

    def error(self, message: str) -> NoReturn:
        """Raise a structured-input exception instead of printing usage."""
        raise CliInputError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line parser."""
    parser = JsonArgumentParser(
        prog="media-scope",
        description=(
            "Resolve a released movie, complete ended series, or latest completed season with TMDb."
        ),
    )
    parser.add_argument("media_type", choices=("movie", "tv"))
    parser.add_argument("title", nargs="?", help="Title to search when --tmdb-id is absent.")
    parser.add_argument("--year", type=_valid_year, help="Release or first-air year.")
    parser.add_argument("--tmdb-id", type=_positive_int, help="Retrieve this exact TMDb record.")
    parser.add_argument("--output", type=Path, help="Also write the resulting JSON to this path.")
    parser.add_argument(
        "--latest-complete-season",
        action="store_true",
        help="For a returning TV series, return its latest provably completed season.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--verbose", action="store_true", help="Enable informational logging.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
) -> int:
    """Run the CLI and return its documented process exit code."""
    configure_utf8_stdio()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.tmdb_id is None and (args.title is None or not args.title.strip()):
            raise CliInputError("title is required unless --tmdb-id is supplied")
        if args.latest_complete_season and args.media_type != "tv":
            raise CliInputError("--latest-complete-season is valid only for television")
    except CliInputError as exc:
        sys.stdout.write(serialize_json(_error_payload(exc.error_code, str(exc))))
        return 2

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    load_dotenv()

    exit_code = 5
    try:
        token = os.getenv("TMDB_BEARER_TOKEN", "")
        if not token.strip():
            raise AuthenticationError(
                "TMDB_BEARER_TOKEN is missing. Configure it in the environment or a .env file."
            )
        factory = client_factory or TmdbClient
        media_type = cast(MediaType, args.media_type)
        if args.tmdb_id is not None:
            LOGGER.info("Resolving %s by TMDb ID %s.", media_type, args.tmdb_id)
        else:
            LOGGER.info("Resolving %s title %r.", media_type, args.title)
        with factory(token) as client:
            details = resolve_media(
                client,
                media_type,
                title=args.title,
                year=args.year,
                tmdb_id=args.tmdb_id,
            )
            payload = (
                build_movie_scope(details)
                if media_type == "movie"
                else build_tv_scope(
                    details,
                    client,
                    mode=(
                        "latest_complete_season"
                        if args.latest_complete_season
                        else "complete_series"
                    ),
                )
            )
        exit_code = 0 if payload["eligible"] else 3
    except AmbiguityError as exc:
        payload = {
            "schema_version": 1,
            "result": "ambiguous",
            "eligible": False,
            "media_type": exc.media_type,
            "query": exc.query,
            "year": exc.year,
            "message": str(exc),
            "candidates": exc.candidates,
            "warnings": [],
        }
        exit_code = 2
    except TmdbError as exc:
        payload = _error_payload(exc.error_code, str(exc))
        exit_code = 4
    except Exception:
        LOGGER.exception("Unexpected internal failure.")
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
            LOGGER.exception("Could not write the requested output file.")
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
        "eligible": False,
        "error_code": error_code,
        "message": message,
        "warnings": [],
    }


def _valid_year(value: str) -> int:
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("year must be an integer") from exc
    if not 1000 <= year <= 9999:
        raise argparse.ArgumentTypeError("year must be between 1000 and 9999")
    return year


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("TMDb ID must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("TMDb ID must be greater than zero")
    return parsed
