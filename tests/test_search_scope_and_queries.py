"""Scope-input validation and deterministic query tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from media_scope.exceptions import SearchInputError, UnsupportedSearchScopeError
from media_scope.scope_input import load_search_scope, validate_search_scope
from media_scope.search_models import SearchScope
from media_scope.search_query_builder import build_search_queries


def completed_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "resolved",
        "eligible": True,
        "media_type": "tv",
        "scope_type": "complete_series",
        "tmdb_id": 1,
        "title": "Example Show",
        "original_title": "Example Show",
        "first_air_year": 2017,
        "status": "Ended",
        "scope": {
            "season_count": 5,
            "seasons": [{"season_number": value} for value in range(1, 6)],
        },
    }


def test_valid_completed_scope_fixture_is_accepted() -> None:
    path = Path(__file__).parent / "fixtures" / "scope_inputs" / "completed_tv.json"
    scope = load_search_scope(path)
    assert scope.tmdb_id == 66573
    assert scope.expected_seasons == (1, 2, 3, 4)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"media_type": "movie"}, UnsupportedSearchScopeError),
        ({"eligible": False}, UnsupportedSearchScopeError),
        ({"status": "Returning Series"}, UnsupportedSearchScopeError),
        ({"scope_type": "latest_complete_season"}, UnsupportedSearchScopeError),
    ],
)
def test_unsupported_scope_is_rejected(
    change: dict[str, Any],
    exception: type[Exception],
) -> None:
    payload = completed_payload()
    payload.update(change)
    with pytest.raises(exception):
        validate_search_scope(payload)


def test_missing_seasons_and_season_zero_are_invalid() -> None:
    payload = completed_payload()
    payload["scope"] = {"season_count": 0, "seasons": []}
    with pytest.raises(SearchInputError):
        validate_search_scope(payload)

    payload = completed_payload()
    payload["scope"]["seasons"][0]["season_number"] = 0
    with pytest.raises(SearchInputError):
        validate_search_scope(payload)


def test_noncontiguous_explicit_seasons_are_preserved() -> None:
    payload = completed_payload()
    payload["scope"] = {
        "season_count": 3,
        "seasons": [
            {"season_number": 5},
            {"season_number": 1},
            {"season_number": 3},
        ],
    }
    assert validate_search_scope(payload).expected_seasons == (1, 3, 5)


def test_duplicate_and_inconsistent_counts_are_rejected() -> None:
    payload = completed_payload()
    payload["scope"]["seasons"][1]["season_number"] = 1
    with pytest.raises(SearchInputError):
        validate_search_scope(payload)

    payload = completed_payload()
    payload["scope"]["season_count"] = 4
    with pytest.raises(SearchInputError):
        validate_search_scope(payload)


def test_missing_optional_title_and_year_produce_warnings() -> None:
    payload = completed_payload()
    payload["original_title"] = None
    payload["first_air_year"] = None
    scope = validate_search_scope(payload)
    assert scope.original_title is None
    assert scope.first_air_year is None
    assert {value["code"] for value in scope.warnings} == {
        "ORIGINAL_TITLE_MISSING",
        "FIRST_AIR_YEAR_MISSING",
    }


def test_five_season_queries_have_exact_deterministic_order() -> None:
    scope = validate_search_scope(completed_payload())
    assert build_search_queries(scope) == (
        "Example Show Complete Series",
        "Example Show Complete",
        "Example Show All Seasons",
        "Example Show S01-S05",
        "Example Show Seasons 1-5",
        "Example Show Season 1-5",
        "Example Show 2017 Complete Series",
    )


def test_one_season_queries_are_complete_series_or_complete_season_queries() -> None:
    scope = SearchScope(1, "One Show", None, 2020, "Ended", (12,))
    assert build_search_queries(scope) == (
        "One Show Complete Series",
        "One Show Complete",
        "One Show All Seasons",
        "One Show S12 Complete",
        "One Show Season 12 Complete",
        "One Show Complete Season 12",
        "One Show 2020 Complete Series",
    )


def test_original_title_adds_only_distinct_high_value_variants() -> None:
    scope = SearchScope(1, "English Name", "Nom Original", 2010, "Ended", (1, 2))
    queries = build_search_queries(scope)
    assert queries[-4:] == (
        "Nom Original Complete Series",
        "Nom Original All Seasons",
        "Nom Original S01-S02",
        "Nom Original 2010 Complete Series",
    )
    assert queries == build_search_queries(scope)


def test_equivalent_original_title_does_not_duplicate_queries() -> None:
    payload = deepcopy(completed_payload())
    payload["original_title"] = "Example.Show"
    assert len(build_search_queries(validate_search_scope(payload))) == 7
