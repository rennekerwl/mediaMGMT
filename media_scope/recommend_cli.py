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
from media_scope.llm_recommendations import (
    SelectionClient,
    SelectionResult,
    build_llm_recommendations,
)
from media_scope.models import JsonObject
from media_scope.openrouter_client import OpenRouterClient
from media_scope.recommendations import (
    MOVIE_TRIGGER_COUNT,
    RECOMMENDATION_COUNT,
    MovieSheetData,
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


class SelectorClientContext(AbstractContextManager[SelectionClient], Protocol):
    """Context-managed selector client returned by the CLI factory."""


SelectorClientFactory = Callable[[str, str], SelectorClientContext]


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="media-recommend",
        description="Write TMDb recommendations when the configured movies folder is low.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable informational logging.")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview LLM recommendations without checking folders or writing files.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    sheet_client_factory: SheetClientFactory | None = None,
    selector_client_factory: SelectorClientFactory | None = None,
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

    engine, llm_config = _recommendation_engine(args.preview)
    if engine is None:
        return 2

    if args.preview:
        return _run_preview(
            llm_config,
            client_factory=client_factory,
            sheet_client_factory=sheet_client_factory,
            selector_client_factory=selector_client_factory,
            today=today,
        )

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

    if engine == "llm":
        llm_config = _read_llm_config()
        if llm_config is None:
            return 2

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
            if engine == "legacy":
                recommendations = build_recommendations(
                    client,
                    sheet_data.ratings,
                    today=today or date.today(),
                    warn=LOGGER.warning,
                    excluded_ids=sheet_data.acquired_ids,
                )
                selection = None
            else:
                selection = _build_llm_selection(
                    client,
                    sheet_data,
                    llm_config,
                    selector_client_factory=selector_client_factory,
                    today=today,
                )
                recommendations = selection.recommendations
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

    if engine == "legacy":
        LOGGER.info(
            "recommendation_method=legacy selected_count=%s fallback=false",
            len(recommendations),
        )
    else:
        assert selection is not None
        _log_llm_selection(selection)

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


def _recommendation_engine(
    preview: bool,
) -> tuple[str | None, tuple[str, str] | None]:
    """Resolve the configured engine, validating LLM credentials for preview."""
    configured = os.getenv("RECOMMENDATION_ENGINE", "legacy").strip().casefold()
    if preview:
        configured = "llm"
    if configured not in {"legacy", "llm"}:
        LOGGER.error(
            "RECOMMENDATION_ENGINE must be either legacy or llm (received %r).", configured
        )
        return None, None
    if configured == "legacy":
        return configured, None

    if not preview:
        return configured, None
    return configured, _read_llm_config()


def _read_llm_config() -> tuple[str, str] | None:
    """Read the OpenRouter credentials required by an LLM selection."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "").strip()
    if not api_key:
        LOGGER.error("OPENROUTER_API_KEY is missing. Configure it in the environment or .env file.")
        return None
    if not model:
        LOGGER.error("OPENROUTER_MODEL is missing. Configure it in the environment or .env file.")
        return None
    return api_key, model


def _run_preview(
    llm_config: tuple[str, str] | None,
    *,
    client_factory: ClientFactory | None,
    sheet_client_factory: SheetClientFactory | None,
    selector_client_factory: SelectorClientFactory | None,
    today: date | None,
) -> int:
    """Run the LLM recommendation flow without any production handoff or file writes."""
    if llm_config is None:
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
            selection = _build_llm_selection(
                client,
                sheet_data,
                llm_config,
                selector_client_factory=selector_client_factory,
                today=today,
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
        LOGGER.exception("Unexpected recommendation preview failure.")
        return 1

    _log_llm_selection(selection)
    payload = _preview_payload(selection, model=llm_config[1])
    sys.stdout.write(serialize_json(payload))
    return 0


def _build_llm_selection(
    client: RecommendationClient,
    sheet_data: MovieSheetData,
    llm_config: tuple[str, str] | None,
    *,
    selector_client_factory: SelectorClientFactory | None,
    today: date | None,
) -> SelectionResult:
    """Build LLM selections with a managed selector client."""
    if llm_config is None:
        raise RecommendationInputError("LLM recommendation credentials are missing.")

    api_key, model = llm_config
    selector_factory = selector_client_factory or OpenRouterClient
    with selector_factory(api_key, model) as selector:
        return build_llm_recommendations(
            client,
            sheet_data.ratings,
            selector=selector,
            today=today or date.today(),
            warn=LOGGER.warning,
            excluded_ids=sheet_data.acquired_ids,
        )


def _log_llm_selection(selection: SelectionResult) -> None:
    """Log selection metadata and bounded reasons without model payloads or secrets."""
    method = str(selection.method)
    fallback = method == "fallback"
    log = LOGGER.warning if fallback else LOGGER.info
    log(
        "recommendation_method=%s candidate_count=%s fallback=%s",
        _sanitize_diagnostic(method),
        selection.candidate_count,
        fallback,
    )
    for tmdb_id, reason in sorted(selection.reasons.items()):
        LOGGER.info(
            "recommendation_reason tmdb_id=%s: %s",
            tmdb_id,
            _sanitize_diagnostic(reason),
        )


def _preview_payload(selection: SelectionResult, *, model: str) -> JsonObject:
    """Build a non-pipeline preview payload using `picks` instead of `recommendations`."""
    picks: list[JsonObject] = [
        {"tmdb_id": item.tmdb_id, "title": item.title, "year": item.year}
        for item in selection.recommendations
    ]
    reasons: JsonObject = {
        str(tmdb_id): _preview_reason(reason)
        for tmdb_id, reason in sorted(selection.reasons.items())
    }
    method = _sanitize_diagnostic(selection.method)
    return {
        "schema_version": 1,
        "result": "recommendations_preview",
        "picks": picks,
        "reasons": reasons,
        "model": _sanitize_diagnostic(model),
        "candidate_count": selection.candidate_count,
        "method": method,
        "fallback": method == "fallback",
    }


def _sanitize_diagnostic(value: object, *, limit: int = 240) -> str:
    """Keep diagnostic strings single-line and bounded before they reach logs or preview JSON."""
    text = _redact_secrets(" ".join(str(value).split()))
    return text[:limit]


def _preview_reason(value: object) -> str:
    """Preserve the complete validated reason while keeping its JSON representation readable."""
    return _redact_secrets(" ".join(str(value).split()))


def _redact_secrets(text: str) -> str:
    """Remove configured credentials from diagnostics before they reach a user-visible channel."""
    for name in ("OPENROUTER_API_KEY", "TMDB_BEARER_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        secret = os.getenv(name, "").strip()
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


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
