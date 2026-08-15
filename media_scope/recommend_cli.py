"""One-shot movies-folder recommendation command."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from media_scope.client import TmdbClient
from media_scope.exceptions import TmdbError
from media_scope.google_sheets import (
    GoogleSheetError,
    GoogleSheetsClient,
    create_google_sheets_client,
)
from media_scope.models import JsonObject
from media_scope.recommendations import (
    MOVIE_TRIGGER_COUNT,
    RECOMMENDATION_COUNT,
    Recommendation,
    RecommendationClient,
    RecommendationInputError,
    build_recommendations,
    count_movies,
    format_recommendations,
    parse_movie_sheet,
)
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.recommend")
RECOMMENDATIONS_FILENAME = "RECOMMENDATIONS.txt"
SheetClientFactory = Callable[[], GoogleSheetsClient]


class ClientContext(AbstractContextManager[RecommendationClient], Protocol):
    """Context-managed recommendation client returned by the CLI factory."""


ClientFactory = Callable[[str], ClientContext]


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="media-recommend",
        description="Write TMDb recommendations when the configured movies folder is low.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable informational logging.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    sheet_client_factory: SheetClientFactory | None = None,
    today: date | None = None,
) -> int:
    """Run one folder check and return zero on success."""
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    load_dotenv()

    movies_text = os.getenv("MOVIES_DIRECTORY", "").strip()
    if not movies_text:
        LOGGER.error("MOVIES_DIRECTORY is missing. Configure it in the environment or .env file.")
        return 2
    movies_directory = Path(movies_text).expanduser()
    if not movies_directory.is_dir():
        LOGGER.error("MOVIES_DIRECTORY does not identify an accessible directory.")
        return 2

    try:
        movie_count = count_movies(movies_directory)
    except OSError:
        LOGGER.exception("Could not inspect MOVIES_DIRECTORY.")
        return 5

    if movie_count >= MOVIE_TRIGGER_COUNT:
        LOGGER.info(
            "Movies folder contains %s entries; no recommendations are needed.", movie_count
        )
        sys.stdout.write(serialize_json(_recommendation_payload([], needed=False)))
        return 0

    recommendations_text = os.getenv("RECOMMENDATIONS_DIRECTORY", "").strip()
    if not recommendations_text:
        LOGGER.error(
            "RECOMMENDATIONS_DIRECTORY is missing. Configure it in the environment or .env file."
        )
        return 2
    recommendations_directory = Path(recommendations_text).expanduser()
    if not recommendations_directory.is_dir():
        LOGGER.error("RECOMMENDATIONS_DIRECTORY does not identify an accessible directory.")
        return 2

    token = os.getenv("TMDB_BEARER_TOKEN", "").strip()
    if not token:
        LOGGER.error("TMDB_BEARER_TOKEN is missing. Configure it in the environment or .env file.")
        return 2

    try:
        sheet_client = (sheet_client_factory or create_google_sheets_client)()
        sheet_data = parse_movie_sheet(sheet_client.read_movie_rows(), LOGGER.warning)
        factory = client_factory or TmdbClient
        with factory(token) as client:
            recommendations = build_recommendations(
                client,
                sheet_data.ratings,
                today=today or date.today(),
                warn=LOGGER.warning,
                excluded_ids=sheet_data.acquired_ids,
            )
    except RecommendationInputError as exc:
        LOGGER.error("%s", exc)
        return 2
    except GoogleSheetError:
        LOGGER.error("Google Sheets ratings could not be read.")
        return 4
    except TmdbError as exc:
        LOGGER.error("TMDb recommendation request failed: %s", exc)
        return 4
    except Exception:
        LOGGER.exception("Unexpected recommendation failure.")
        return 1

    if len(recommendations) < RECOMMENDATION_COUNT:
        LOGGER.warning(
            "TMDb supplied only %s of the %s requested recommendation(s).",
            len(recommendations),
            RECOMMENDATION_COUNT,
        )

    output = recommendations_directory / RECOMMENDATIONS_FILENAME
    temporary_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=recommendations_directory,
            prefix=f".{RECOMMENDATIONS_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(format_recommendations(recommendations))
            temporary_output = Path(handle.name)
        temporary_output.replace(output)
    except OSError:
        if temporary_output is not None:
            try:
                temporary_output.unlink(missing_ok=True)
            except OSError:
                pass
        LOGGER.exception("Could not write %s.", RECOMMENDATIONS_FILENAME)
        return 5

    LOGGER.info("Wrote %s recommendation(s) to %s.", len(recommendations), output)
    sys.stdout.write(serialize_json(_recommendation_payload(recommendations, needed=True)))
    return 0


def _recommendation_payload(
    recommendations: Sequence[Recommendation],
    *,
    needed: bool,
) -> JsonObject:
    """Build the stable machine handoff for the movie-search stage."""
    values: list[JsonObject] = [
        {"tmdb_id": item.tmdb_id, "title": item.title, "year": item.year}
        for item in recommendations
    ]
    return {
        "schema_version": 1,
        "result": "recommendations_created" if needed else "recommendations_not_needed",
        "recommendations": values,
    }


if __name__ == "__main__":
    raise SystemExit(main())
