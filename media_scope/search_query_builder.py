"""Deterministic complete-series query generation."""

from __future__ import annotations

from media_scope.resolver import normalize_title
from media_scope.search_models import SearchScope


def build_search_queries(scope: SearchScope) -> tuple[str, ...]:
    """Build a small ordered collection of high-value complete-series queries."""
    queries = _queries_for_title(
        scope.title,
        scope.expected_seasons,
        scope.first_air_year,
        original_variant=False,
    )
    if scope.original_title and normalize_title(scope.original_title) != normalize_title(
        scope.title
    ):
        queries.extend(
            _queries_for_title(
                scope.original_title,
                scope.expected_seasons,
                scope.first_air_year,
                original_variant=True,
            )
        )

    deduplicated: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = normalize_title(query)
        if key not in seen:
            seen.add(key)
            deduplicated.append(query)
    return tuple(deduplicated)


def _queries_for_title(
    title: str,
    seasons: tuple[int, ...],
    year: int | None,
    *,
    original_variant: bool,
) -> list[str]:
    minimum = min(seasons)
    maximum = max(seasons)
    if len(seasons) == 1:
        season_notation = f"S{minimum:02d} Complete"
        if original_variant:
            values = [
                f"{title} Complete Series",
                f"{title} All Seasons",
                f"{title} {season_notation}",
            ]
        else:
            values = [
                f"{title} Complete Series",
                f"{title} Complete",
                f"{title} All Seasons",
                f"{title} {season_notation}",
                f"{title} Season {minimum} Complete",
                f"{title} Complete Season {minimum}",
            ]
    else:
        season_notation = f"S{minimum:02d}-S{maximum:02d}"
        if original_variant:
            values = [
                f"{title} Complete Series",
                f"{title} All Seasons",
                f"{title} {season_notation}",
            ]
        else:
            values = [
                f"{title} Complete Series",
                f"{title} Complete",
                f"{title} All Seasons",
                f"{title} {season_notation}",
                f"{title} Seasons {minimum}-{maximum}",
                f"{title} Season {minimum}-{maximum}",
            ]
    if year is not None:
        values.append(f"{title} {year} Complete Series")
    return values
