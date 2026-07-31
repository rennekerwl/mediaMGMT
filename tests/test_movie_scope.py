"""Movie eligibility and scope tests."""

from __future__ import annotations

from typing import Any

from media_scope.scope_builder import build_movie_scope


def test_released_movie_is_eligible(load_fixture: Any) -> None:
    result = build_movie_scope(load_fixture("movie_released.json"))

    assert result["eligible"] is True
    assert result["scope_type"] == "single_movie"
    assert result["scope"] == {"movie_count": 1}
    assert result["release_year"] == 1982
    assert result["runtime_minutes"] == 109


def test_unreleased_movie_is_ineligible(load_fixture: Any) -> None:
    result = build_movie_scope(load_fixture("movie_unreleased.json"))

    assert result["eligible"] is False
    assert result["ineligibility_code"] == "MOVIE_NOT_RELEASED"
    assert "scope" not in result
