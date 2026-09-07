"""Tests for the taste-aware recommendation collection and selection pipeline."""

from __future__ import annotations

from datetime import date
from typing import Any

from media_scope.exceptions import TmdbError
from media_scope.llm_recommendations import (
    CandidatePool,
    build_llm_recommendations,
    fallback_selection,
    validate_selection,
)
from media_scope.recommendations import RatingRow


def movie(
    tmdb_id: int,
    title: str,
    *,
    popularity: float = 10,
    vote_average: float = 8,
    vote_count: int = 1000,
    genre_ids: list[int] | None = None,
    overview: str = "A synopsis.",
) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "popularity": popularity,
        "vote_average": vote_average,
        "vote_count": vote_count,
        "release_date": "2020-01-02",
        "adult": False,
        "video": False,
        "genre_ids": genre_ids or [18],
        "overview": overview,
    }


class FakeTmdb:
    def __init__(
        self,
        *,
        recommendations: dict[int, list[dict[str, Any]]] | None = None,
        discoveries: dict[int, list[dict[str, Any]]] | None = None,
        searches: dict[str, list[dict[str, Any]]] | None = None,
        details: dict[int, object] | None = None,
    ) -> None:
        self.recommendations = recommendations or {}
        self.discoveries = discoveries or {}
        self.searches = searches or {}
        self.details = details or {}
        self.recommendation_calls: list[int] = []
        self.discovery_calls: list[int] = []
        self.search_calls: list[tuple[str, int | None]] = []
        self.detail_calls: list[int] = []

    def search_movies(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        self.search_calls.append((title, year))
        return self.searches.get(title, [])

    def get_movie_recommendations(self, tmdb_id: int) -> list[dict[str, Any]]:
        self.recommendation_calls.append(tmdb_id)
        return self.recommendations.get(tmdb_id, [])

    def discover_movies(
        self,
        *,
        page: int,
        released_through: date,
        min_vote_average: float,
        min_vote_count: int,
    ) -> list[dict[str, Any]]:
        del released_through, min_vote_average, min_vote_count
        self.discovery_calls.append(page)
        return self.discoveries.get(page, [])

    def get_movie(self, tmdb_id: int) -> dict[str, Any]:
        self.detail_calls.append(tmdb_id)
        detail = self.details.get(tmdb_id, {})
        if isinstance(detail, BaseException):
            raise detail
        return detail  # type: ignore[return-value]


class FakeSelector:
    def __init__(self, selection: object) -> None:
        self.selection = selection
        self.calls: list[dict[str, Any]] = []

    def select(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return self.selection


def test_llm_uses_full_history_and_deduplicated_candidates() -> None:
    client = FakeTmdb(
        recommendations={
            1: [movie(10, "Ten"), movie(11, "Eleven")],
            2: [movie(10, "Ten duplicate"), movie(12, "Twelve")],
        },
        discoveries={1: [movie(13, "Thirteen")], 2: [movie(14, "Fourteen")], 3: []},
        details={
            1: {"overview": "Loved synopsis", "genres": [{"id": 28, "name": "Action"}]},
            2: {"overview": "Hated synopsis", "genres": [{"id": 35, "name": "Comedy"}]},
            3: {"overview": "Neutral synopsis", "genres": []},
        },
    )
    selector = FakeSelector(
        [
            {"tmdb_id": 11, "reason": "A good fit."},
            {"tmdb_id": 10, "reason": "Shares the right tone."},
            {"tmdb_id": 13, "reason": "A quality exploration."},
        ]
    )

    result = build_llm_recommendations(
        client,
        [
            RatingRow("Loved", None, 5, 2, 1, "bright"),
            RatingRow("Disliked", None, 1, 3, 2, "too grim"),
            RatingRow("Neutral", None, 3, 4, 3, "mixed"),
        ],
        selector=selector,
        today=date(2030, 1, 1),
        warn=lambda _message: None,
    )

    assert result.method == "llm"
    assert result.candidate_count == 4
    assert [item.tmdb_id for item in result.recommendations] == [11, 10, 13]
    assert client.recommendation_calls == [1]
    assert client.discovery_calls == [1, 2, 3]
    assert client.detail_calls == [1, 2, 3]
    assert {item["tmdb_id"] for item in selector.calls[0]["candidates"]} == {10, 11, 13, 14}
    assert [(item["rating"], item["notes"]) for item in selector.calls[0]["taste_history"]] == [
        (5, "bright"),
        (1, "too grim"),
        (3, "mixed"),
    ]
    assert selector.calls[0]["candidates"][0].keys() >= {
        "tmdb_id",
        "title",
        "year",
        "overview",
        "genres",
        "vote_average",
        "vote_count",
    }
    assert "weighted_support" not in selector.calls[0]["candidates"][0]


def test_candidate_enrichment_is_not_fetched_and_discovery_thresholds_are_checked() -> None:
    client = FakeTmdb(
        recommendations={1: [movie(10, "Seed Candidate")]},
        discoveries={
            1: [movie(20, "Low Score", vote_average=6.9), movie(21, "Low Votes", vote_count=499)],
            2: [movie(22, "Good")],
            3: [],
        },
        details={1: {"overview": "Seed"}},
    )
    selector = FakeSelector(
        [
            {"tmdb_id": 10, "reason": "Only seed candidate."},
            {"tmdb_id": 22, "reason": "Quality discovery."},
        ]
    )

    result = build_llm_recommendations(
        client,
        [RatingRow("Seed", None, 5, 2, 1)],
        selector=selector,
        today=date(2030, 1, 1),
        warn=lambda _message: None,
    )

    assert result.candidate_count == 2
    assert [item.tmdb_id for item in result.recommendations] == [10, 22]
    assert client.detail_calls == [1]
    assert client.discovery_calls == [1, 2, 3]


def test_resolution_keeps_unresolved_rating_as_basic_history() -> None:
    client = FakeTmdb(recommendations={}, discoveries={1: []})
    selector = FakeSelector([])

    result = build_llm_recommendations(
        client,
        [RatingRow("Unknown", 2020, 2, 2, notes="skip this")],
        selector=selector,
        today=date(2030, 1, 1),
        warn=lambda _message: None,
    )

    assert result.candidate_count == 0
    assert selector.calls == []
    assert client.search_calls == [("Unknown", 2020)]


def test_get_movie_failure_keeps_basic_history_and_is_cached() -> None:
    client = FakeTmdb(
        recommendations={1: [movie(10, "Candidate")]},
        discoveries={1: []},
        details={1: TmdbError("unavailable")},
    )
    selector = FakeSelector([{"tmdb_id": 10, "reason": "Fits."}])

    build_llm_recommendations(
        client,
        [RatingRow("Seed", None, 5, 2, 1), RatingRow("Seed again", None, 4, 3, 1)],
        selector=selector,
        today=date(2030, 1, 1),
        warn=lambda _message: None,
    )

    assert client.detail_calls == [1]
    assert selector.calls[0]["taste_history"][0] == {
        "tmdb_id": 1,
        "title": "Seed",
        "year": None,
        "rating": 5,
        "notes": "",
    }


def test_invalid_selection_falls_back_with_source_reasons() -> None:
    client = FakeTmdb(
        recommendations={
            1: [movie(10, "First"), movie(11, "Second")],
            2: [movie(10, "Shared"), movie(12, "Third")],
        },
        discoveries={1: [movie(20, "Discovery")], 2: []},
    )
    selector = FakeSelector([{"tmdb_id": 999, "reason": "Unknown."}])
    warnings: list[str] = []

    result = build_llm_recommendations(
        client,
        [RatingRow("Seed one", None, 4, 2, 1), RatingRow("Seed two", None, 5, 3, 2)],
        selector=selector,
        today=date(2030, 1, 1),
        warn=warnings.append,
    )

    assert result.method == "fallback"
    assert [item.tmdb_id for item in result.recommendations] == [10, 11, 20]
    assert "TMDb recommendations" in result.reasons[10]
    assert "TMDb recommendations" in result.reasons[11]
    assert "TMDb discovery" in result.reasons[20]
    assert warnings


def test_fallback_round_robin_places_discovery_after_each_seed_turn() -> None:
    seed_a = {"tmdb_id": 10, "title": "A"}
    seed_b = {"tmdb_id": 20, "title": "B"}
    discovery = {"tmdb_id": 30, "title": "D"}
    pool = CandidatePool(
        candidates=[seed_a, seed_b, discovery],
        seed_lists=((1, (seed_a,)), (2, (seed_b,))),
        discovery=(discovery,),
    )

    assert [
        item["tmdb_id"] for item in fallback_selection(pool, seed_ratings={1: 5, 2: 4}, count=3)
    ] == [
        10,
        20,
        30,
    ]


def test_selection_validation_requires_membership_unique_ids_and_reasons() -> None:
    candidate = {
        "tmdb_id": 10,
        "title": "Movie",
        "year": 2020,
        "overview": "",
        "genres": [18],
        "genre_ids": [18],
        "popularity": 1.0,
        "vote_average": 7.0,
        "vote_count": 500,
    }

    for invalid in (
        [{"tmdb_id": 999, "reason": "No"}],
        [{"tmdb_id": 10, "reason": ""}],
        [{"tmdb_id": 10, "reason": "One"}, {"tmdb_id": 10, "reason": "Two"}],
    ):
        try:
            validate_selection(invalid, candidates=[candidate], count=len(invalid))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid selector output was accepted")
