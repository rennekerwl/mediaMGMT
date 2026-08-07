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

import httpx
from dotenv import load_dotenv

from media_scope.client import TmdbClient
from media_scope.exceptions import TmdbError
from media_scope.recommendations import (
    MOVIE_TRIGGER_COUNT,
    RECOMMENDATION_COUNT,
    RecommendationClient,
    RecommendationInputError,
    build_recommendations,
    count_movies,
    format_recommendations,
    parse_ratings_csv,
)
from media_scope.serialization import configure_utf8_stdio

LOGGER = logging.getLogger("media_scope.recommend")
RECOMMENDATIONS_FILENAME = "RECOMMENDATIONS.txt"
CsvFetcher = Callable[[str], str]


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


def fetch_published_csv(url: str) -> str:
    """Download one published Google Sheet CSV."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise RecommendationInputError(
            "The published Google Sheet CSV could not be downloaded."
        ) from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    csv_fetcher: CsvFetcher = fetch_published_csv,
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

    csv_url = os.getenv("GOOGLE_SHEET_CSV_URL", "").strip()
    token = os.getenv("TMDB_BEARER_TOKEN", "").strip()
    if not csv_url:
        LOGGER.error(
            "GOOGLE_SHEET_CSV_URL is missing. Configure it in the environment or .env file."
        )
        return 2
    if not token:
        LOGGER.error("TMDB_BEARER_TOKEN is missing. Configure it in the environment or .env file.")
        return 2

    try:
        ratings = parse_ratings_csv(csv_fetcher(csv_url), LOGGER.warning)
        factory = client_factory or TmdbClient
        with factory(token) as client:
            recommendations = build_recommendations(
                client,
                ratings,
                today=today or date.today(),
                warn=LOGGER.warning,
            )
    except RecommendationInputError as exc:
        LOGGER.error("%s", exc)
        return 2
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
