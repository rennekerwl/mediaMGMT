"""Movie-folder counting, Sheet parsing, and recommendation ranking tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from media_scope.recommendations import (
    RatingRow,
    RecommendationInputError,
    build_recommendations,
    count_movies,
    format_recommendations,
    parse_ratings_csv,
)


class FakeRecommendationClient:
    def __init__(
        self,
        searches: dict[str, list[dict[str, Any]]],
        recommendations: dict[int, list[dict[str, Any]]],
    ) -> None:
        self.searches = searches
        self.recommendations = recommendations
        self.search_calls: list[tuple[str, int | None]] = []
        self.recommendation_calls: list[int] = []

    def search_movies(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        self.search_calls.append((title, year))
        return self.searches.get(title, [])

    def get_movie_recommendations(self, tmdb_id: int) -> list[dict[str, Any]]:
        self.recommendation_calls.append(tmdb_id)
        return self.recommendations.get(tmdb_id, [])


def movie(
    tmdb_id: int,
    title: str,
    *,
    popularity: float,
    vote_average: float = 7.0,
    release_date: str = "2020-01-02",
    adult: bool = False,
) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "popularity": popularity,
        "vote_average": vote_average,
        "release_date": release_date,
        "adult": adult,
    }


def test_count_movies_counts_top_level_video_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "Movie One").mkdir()
    (tmp_path / "movie-two.MKV").touch()
    (tmp_path / "poster.jpg").touch()
    (tmp_path / "RECOMMENDATIONS.txt").touch()
    nested = tmp_path / "Movie One" / "extras"
    nested.mkdir(parents=True)
    (nested / "feature.mp4").touch()

    assert count_movies(tmp_path) == 2


def test_parse_ratings_accepts_case_insensitive_headers_and_optional_year() -> None:
    warnings: list[str] = []
    csv_text = " title , YEAR , rating ,Notes\nArrival,2016,4,great\nAlien,,3,okay\n"

    assert parse_ratings_csv(csv_text, warnings.append) == [
        RatingRow("Arrival", 2016, 4, 2),
        RatingRow("Alien", None, 3, 3),
    ]
    assert warnings == []


def test_parse_ratings_warns_and_skips_malformed_rows() -> None:
    warnings: list[str] = []
    csv_text = (
        "Title,Year,Rating\n,1999,5\nBad Rating,2000,no\nDecimal,2001,4.5\n"
        "Bad Year,twenty,4\nLow,,0\n"
    )

    assert parse_ratings_csv(csv_text, warnings.append) == []
    assert len(warnings) == 5
    assert all("skipping row" in warning for warning in warnings)


def test_rating_meanings_and_positive_seed_cutoff() -> None:
    rows = [RatingRow("Example", None, rating, 2) for rating in range(1, 6)]

    assert [row.sentiment for row in rows] == [
        "hated it",
        "disliked it",
        "neutral",
        "liked it",
        "loved it",
    ]
    assert [row.liked for row in rows] == [False, False, False, True, True]


def test_parse_ratings_requires_title_and_rating_headers() -> None:
    with pytest.raises(RecommendationInputError, match="Rating"):
        parse_ratings_csv("Title,Year\nArrival,2016\n", lambda _message: None)


def test_build_recommendations_resolves_by_popularity_filters_and_ranks() -> None:
    warnings: list[str] = []
    client = FakeRecommendationClient(
        searches={
            "Liked One": [{"id": 9, "popularity": 1}, {"id": 10, "popularity": 8}],
            "Liked Two": [{"id": 20, "popularity": 4}],
            "Disliked": [{"id": 30, "popularity": 5}],
            "Missing": [],
        },
        recommendations={
            10: [
                movie(100, "Consensus", popularity=5),
                movie(101, "Popular", popularity=100),
                movie(30, "Already Rated", popularity=500),
                movie(103, "Adult", popularity=500, adult=True),
                movie(104, "Future", popularity=500, release_date="2031-01-01"),
                {"id": 105, "title": "Incomplete"},
            ],
            20: [
                movie(100, "Consensus", popularity=6),
                movie(102, "Less Popular", popularity=50),
                movie(100, "Consensus Duplicate", popularity=1000),
            ],
        },
    )
    ratings = [
        RatingRow("Liked One", 2001, 4, 2),
        RatingRow("Liked Two", None, 5, 3),
        RatingRow("Disliked", None, 2, 4),
        RatingRow("Missing", None, 4, 5),
    ]

    result = build_recommendations(
        client,
        ratings,
        limit=3,
        today=date(2030, 1, 1),
        warn=warnings.append,
    )

    assert [item.tmdb_id for item in result] == [100, 101, 102]
    assert result[0].support == 2
    assert client.recommendation_calls == [10, 20]
    assert client.search_calls[0] == ("Liked One", 2001)
    assert len(warnings) == 1
    assert "Missing" in warnings[0]
    assert format_recommendations(result) == (
        "Consensus (2020)\nPopular (2020)\nLess Popular (2020)\n"
    )


def test_ranking_uses_vote_average_then_tmdb_id_for_ties() -> None:
    client = FakeRecommendationClient(
        searches={"Seed": [{"id": 1, "popularity": 1}]},
        recommendations={
            1: [
                movie(5, "Later ID", popularity=10, vote_average=8),
                movie(4, "Lower Vote", popularity=10, vote_average=7),
                movie(3, "Earlier ID", popularity=10, vote_average=8),
            ]
        },
    )

    result = build_recommendations(
        client,
        [RatingRow("Seed", None, 5, 2)],
        limit=3,
        today=date(2030, 1, 1),
        warn=lambda _message: None,
    )

    assert [item.tmdb_id for item in result] == [3, 5, 4]
