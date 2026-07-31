"""Strict title and ID resolution tests."""

from __future__ import annotations

from typing import Any

import pytest

from media_scope.exceptions import AmbiguityError, NotFoundError
from media_scope.resolver import normalize_title, resolve_media


class ResolverClient:
    """Small resolver double that records the requested endpoints."""

    def __init__(
        self,
        movie_results: list[dict[str, Any]] | None = None,
        tv_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.movie_results = movie_results or []
        self.tv_results = tv_results or []
        self.calls: list[tuple[Any, ...]] = []

    def search_movies(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("search_movies", title, year))
        return self.movie_results

    def search_tv(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("search_tv", title, year))
        return self.tv_results

    def get_movie(self, tmdb_id: int) -> dict[str, Any]:
        self.calls.append(("get_movie", tmdb_id))
        return {"id": tmdb_id}

    def get_tv(self, tmdb_id: int) -> dict[str, Any]:
        self.calls.append(("get_tv", tmdb_id))
        return {"id": tmdb_id}


def movie_result(tmdb_id: int, title: str, year: int) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": f"{year}-01-01",
        "overview": f"Overview for {title}",
    }


def test_movie_resolves_by_exact_tmdb_id_without_search() -> None:
    client = ResolverClient()

    result = resolve_media(client, "movie", title="Ignored", year=1982, tmdb_id=1091)  # type: ignore[arg-type]

    assert result == {"id": 1091}
    assert client.calls == [("get_movie", 1091)]


def test_movie_resolves_by_unique_title_and_year() -> None:
    client = ResolverClient(
        [movie_result(1091, "The Thing", 1982), movie_result(1, "The Thing", 2011)]
    )

    result = resolve_media(client, "movie", title="The Thing", year=1982)  # type: ignore[arg-type]

    assert result == {"id": 1091}
    assert client.calls[0] == ("search_movies", "The Thing", 1982)
    assert client.calls[1] == ("get_movie", 1091)


def test_tv_resolves_by_unique_title_and_first_air_year() -> None:
    client = ResolverClient(
        tv_results=[
            {
                "id": 66573,
                "name": "The Good Place",
                "original_name": "The Good Place",
                "first_air_date": "2016-09-19",
                "overview": "A comedy.",
            }
        ]
    )

    result = resolve_media(client, "tv", title="The Good Place", year=2016)  # type: ignore[arg-type]

    assert result == {"id": 66573}
    assert client.calls == [
        ("search_tv", "The Good Place", 2016),
        ("get_tv", 66573),
    ]


def test_missing_original_title_can_still_match_display_title() -> None:
    result_without_original = movie_result(1091, "The Thing", 1982)
    del result_without_original["original_title"]
    client = ResolverClient([result_without_original])

    result = resolve_media(client, "movie", title="The Thing", year=1982)  # type: ignore[arg-type]

    assert result == {"id": 1091}


def test_ambiguous_movie_requires_tmdb_id_and_limits_candidates() -> None:
    results = [movie_result(index, "King Kong", 1933) for index in range(1, 13)]
    client = ResolverClient(results)

    with pytest.raises(AmbiguityError) as caught:
        resolve_media(client, "movie", title="King Kong", year=1933)  # type: ignore[arg-type]

    assert len(caught.value.candidates) == 10
    assert "--tmdb-id" in str(caught.value)
    assert not any(call[0] == "get_movie" for call in client.calls)


def test_near_match_is_not_silently_selected() -> None:
    client = ResolverClient([movie_result(1, "The Thing Returns", 1982)])

    with pytest.raises(AmbiguityError):
        resolve_media(client, "movie", title="The Thing", year=1982)  # type: ignore[arg-type]


def test_empty_search_is_not_found() -> None:
    client = ResolverClient()

    with pytest.raises(NotFoundError):
        resolve_media(client, "movie", title="Unknown")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("  THE   THING ", "the thing"),
        ("Spider-Man", "spider man"),
        ("Schindler’s List", "Schindlers List"),
        ("Mission: Impossible", "mission impossible"),
    ],
)
def test_title_normalization_handles_common_differences(left: str, right: str) -> None:
    assert normalize_title(left) == normalize_title(right)
