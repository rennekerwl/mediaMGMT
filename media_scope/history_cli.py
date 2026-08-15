"""Record a verified movie transfer in Google Sheets acquisition history."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any

from dotenv import load_dotenv

from media_scope.google_sheets import (
    GoogleSheetError,
    GoogleSheetsClient,
    create_google_sheets_client,
)
from media_scope.models import JsonObject
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.history")
HistoryClientFactory = Callable[[], GoogleSheetsClient]


class MovieHistoryInputError(ValueError):
    """Raised when a transfer artifact cannot prove a completed local copy."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-record-movie",
        description="Record one verified movie transfer in Google Sheets.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--verbose", action="store_true", help="Enable informational logging.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    input_text: str | None = None,
    client_factory: HistoryClientFactory | None = None,
) -> int:
    """Read one transfer result, idempotently record it, and emit one JSON object."""
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    load_dotenv()

    try:
        movie = parse_history_input(input_text if input_text is not None else sys.stdin.read())
        client = (client_factory or create_google_sheets_client)()
        appended = client.record_acquired_movie(
            tmdb_id=int(movie["tmdb_id"]),
            title=str(movie["title"]),
            year=int(movie["year"]),
        )
        result = "movie_history_recorded" if appended else "movie_history_already_recorded"
        payload: JsonObject = {
            "schema_version": 1,
            "result": result,
            "movie": movie,
            "message": (
                "The acquired movie was added to Google Sheets."
                if appended
                else "The acquired movie was already present in Google Sheets."
            ),
        }
        exit_code = 0
    except MovieHistoryInputError as exc:
        payload = _error_payload("INVALID_TRANSFER_RESULT", str(exc))
        exit_code = 2
    except GoogleSheetError as exc:
        LOGGER.error("Google Sheets acquisition-history update failed.")
        payload = _error_payload(
            exc.error_code, "Google Sheets acquisition history could not be updated."
        )
        exit_code = 4
    except Exception:
        LOGGER.error("Unexpected movie-history failure.")
        payload = _error_payload(
            "MOVIE_HISTORY_INTERNAL_ERROR", "An unexpected movie-history error occurred."
        )
        exit_code = 5

    sys.stdout.write(serialize_json(payload, pretty=args.pretty))
    return exit_code


def parse_history_input(text: str) -> JsonObject:
    """Validate the minimum proof required to record a completed local transfer."""
    try:
        value: Any = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MovieHistoryInputError("Transfer input must be one valid JSON object.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise MovieHistoryInputError("Transfer input must use schema_version 1.")
    if value.get("result") not in {
        "transfer_completed",
        "transfer_completed_cleanup_failed",
    }:
        raise MovieHistoryInputError("Transfer input does not report a completed local copy.")
    transfer = value.get("transfer")
    if not isinstance(transfer, dict) or transfer.get("status") != "COMPLETED":
        raise MovieHistoryInputError("Transfer input does not verify a completed local copy.")
    scope = value.get("scope")
    if not isinstance(scope, dict) or scope.get("media_type") != "movie":
        raise MovieHistoryInputError("Transfer input must contain a movie scope.")
    tmdb_id = _positive_int(scope.get("tmdb_id"))
    title = scope.get("title")
    year = scope.get("year")
    if tmdb_id is None or not isinstance(title, str) or not title.strip():
        raise MovieHistoryInputError("Transfer movie identity is incomplete.")
    if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
        raise MovieHistoryInputError("Transfer movie year is invalid.")
    return {"tmdb_id": tmdb_id, "title": title.strip(), "year": year}


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _error_payload(error_code: str, message: str) -> JsonObject:
    return {
        "schema_version": 1,
        "result": "movie_history_failed",
        "error_code": error_code,
        "message": message,
    }


if __name__ == "__main__":
    raise SystemExit(main())
