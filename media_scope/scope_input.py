"""Validation for JSON produced by the TMDb television scope component."""

from __future__ import annotations

import json
from pathlib import Path

from media_scope.exceptions import SearchInputError, UnsupportedSearchScopeError
from media_scope.models import JsonObject
from media_scope.search_models import SearchScope, json_warning


def load_search_scope(path: Path) -> SearchScope:
    """Load and validate one complete-ended-TV scope file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SearchInputError(f"Scope file does not exist: {path}") from exc
    except OSError as exc:
        raise SearchInputError(f"Scope file could not be read: {path}") from exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SearchInputError("Scope file is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise SearchInputError("Scope JSON must contain one object.")
    return validate_search_scope(payload)


def validate_search_scope(payload: JsonObject) -> SearchScope:
    """Validate the existing scope schema without silently repairing it."""
    if payload.get("schema_version") != 1 or payload.get("result") != "resolved":
        raise SearchInputError("Scope JSON must be a resolved schema-version 1 record.")
    if payload.get("media_type") != "tv":
        raise UnsupportedSearchScopeError("Only television scopes are supported.")
    if payload.get("eligible") is not True:
        raise UnsupportedSearchScopeError("The television scope is not eligible.")
    if payload.get("scope_type") != "complete_series":
        raise UnsupportedSearchScopeError("Only complete-series scopes are supported.")
    if payload.get("status") != "Ended":
        raise UnsupportedSearchScopeError("Only television series with status Ended are supported.")

    tmdb_id = payload.get("tmdb_id")
    title = payload.get("title")
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise SearchInputError("Scope JSON must contain a positive integer tmdb_id.")
    if not isinstance(title, str) or not title.strip():
        raise SearchInputError("Scope JSON must contain a nonempty title.")

    scope_value = payload.get("scope")
    if not isinstance(scope_value, dict):
        raise SearchInputError("Scope JSON must contain a scope object.")
    seasons_value = scope_value.get("seasons")
    if not isinstance(seasons_value, list) or not seasons_value:
        raise SearchInputError("Scope JSON must contain at least one regular season.")

    season_numbers: list[int] = []
    for value in seasons_value:
        if not isinstance(value, dict):
            raise SearchInputError("Every scope season must be an object.")
        season_number = value.get("season_number")
        if (
            not isinstance(season_number, int)
            or isinstance(season_number, bool)
            or season_number <= 0
        ):
            raise SearchInputError("Every included season number must be a positive integer.")
        season_numbers.append(season_number)
    if len(set(season_numbers)) != len(season_numbers):
        raise SearchInputError("Included regular season numbers must be unique.")

    season_count = scope_value.get("season_count")
    if (
        not isinstance(season_count, int)
        or isinstance(season_count, bool)
        or season_count != len(season_numbers)
    ):
        raise SearchInputError("scope.season_count does not match the explicit season list.")

    warnings: list[JsonObject] = []
    original_value = payload.get("original_title")
    original_title = (
        original_value.strip()
        if isinstance(original_value, str) and original_value.strip()
        else None
    )
    if original_title is None:
        warnings.append(
            json_warning(
                "ORIGINAL_TITLE_MISSING",
                "The scope has no original title; only the display title will be searched.",
            )
        )

    year_value = payload.get("first_air_year")
    first_air_year = (
        year_value
        if isinstance(year_value, int)
        and not isinstance(year_value, bool)
        and 1000 <= year_value <= 9999
        else None
    )
    if first_air_year is None:
        warnings.append(
            json_warning(
                "FIRST_AIR_YEAR_MISSING",
                "The scope has no valid first-air year; year variants will be omitted.",
            )
        )

    return SearchScope(
        tmdb_id=tmdb_id,
        title=title.strip(),
        original_title=original_title,
        first_air_year=first_air_year,
        status="Ended",
        expected_seasons=tuple(sorted(season_numbers)),
        warnings=tuple(warnings),
    )
