"""Small, deterministic movie-recommendation workflow helpers."""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from media_scope.models import JsonObject

MOVIE_TRIGGER_COUNT = 3
RECOMMENDATION_COUNT = 3
DISCOVERY_MAX_PAGES = 3
DISCOVERY_MIN_VOTE_AVERAGE = 7.0
DISCOVERY_MIN_VOTE_COUNT = 500
VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm", ".wmv"})
RATING_LABELS = {
    1: "hated it",
    2: "disliked it",
    3: "neutral",
    4: "liked it",
    5: "loved it",
}

WarningHandler = Callable[[str], None]


class RecommendationInputError(Exception):
    """Raised when recommendation configuration or CSV input is unusable."""


class RecommendationClient(Protocol):
    """TMDb operations used by the recommendation workflow."""

    def search_movies(self, title: str, year: int | None = None) -> list[JsonObject]: ...

    def get_movie_recommendations(self, tmdb_id: int) -> list[JsonObject]: ...

    def discover_movies(
        self,
        *,
        page: int,
        released_through: date,
        min_vote_average: float,
        min_vote_count: int,
    ) -> list[JsonObject]: ...


@dataclass(frozen=True)
class RatingRow:
    """One valid rating imported from the published Sheet."""

    title: str
    year: int | None
    rating: int
    row_number: int

    @property
    def sentiment(self) -> str:
        """Return the configured meaning of this rating."""
        return RATING_LABELS[self.rating]

    @property
    def liked(self) -> bool:
        """Return whether this rating should seed TMDb recommendations."""
        return self.rating >= 4


@dataclass(frozen=True)
class Recommendation:
    """One ranked, printable recommendation."""

    tmdb_id: int
    title: str
    year: int
    weighted_support: float
    genre_ids: frozenset[int]
    popularity: float
    vote_average: float
    vote_count: int


def count_movies(directory: Path) -> int:
    """Count immediate movie directories and recognized top-level video files."""
    return sum(
        1
        for entry in directory.iterdir()
        if entry.is_dir() or (entry.is_file() and entry.suffix.casefold() in VIDEO_EXTENSIONS)
    )


def parse_ratings_csv(csv_text: str, warn: WarningHandler) -> list[RatingRow]:
    """Parse valid rating rows and warn about malformed individual rows."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise RecommendationInputError("The Google Sheet CSV has no header row.")

    headers = {
        header.strip().casefold(): header for header in reader.fieldnames if header is not None
    }
    missing = {"title", "rating"} - headers.keys()
    if missing:
        names = ", ".join(sorted(name.title() for name in missing))
        raise RecommendationInputError(
            f"The Google Sheet CSV is missing required column(s): {names}."
        )

    title_header = headers["title"]
    rating_header = headers["rating"]
    year_header = headers.get("year")
    ratings: list[RatingRow] = []

    for row_number, row in enumerate(reader, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue

        title = (row.get(title_header) or "").strip()
        rating_text = (row.get(rating_header) or "").strip()
        if not title:
            warn(f"Sheet row {row_number}: missing Title; skipping row.")
            continue
        try:
            rating = int(rating_text)
        except ValueError:
            warn(
                f"Sheet row {row_number}: Rating must be a whole number from 1 to 5; skipping row."
            )
            continue
        if rating not in RATING_LABELS:
            warn(
                f"Sheet row {row_number}: Rating must be a whole number from 1 to 5; skipping row."
            )
            continue

        year: int | None = None
        if year_header is not None:
            year_text = (row.get(year_header) or "").strip()
            if year_text:
                try:
                    year = int(year_text)
                except ValueError:
                    warn(f"Sheet row {row_number}: Year must be a four-digit year; skipping row.")
                    continue
                if not 1000 <= year <= 9999:
                    warn(f"Sheet row {row_number}: Year must be a four-digit year; skipping row.")
                    continue

        ratings.append(RatingRow(title=title, year=year, rating=rating, row_number=row_number))

    return ratings


def build_recommendations(
    client: RecommendationClient,
    ratings: list[RatingRow],
    *,
    today: date,
    warn: WarningHandler,
) -> list[Recommendation]:
    """Build two personalized choices followed by one broader exploration choice."""
    rated_ids: set[int] = set()
    seed_weights: dict[int, float] = {}

    for rating in ratings:
        results = client.search_movies(rating.title, rating.year)
        resolved = _most_popular_valid_result(results)
        if resolved is None:
            suffix = f" ({rating.year})" if rating.year is not None else ""
            warn(
                f"Sheet row {rating.row_number}: TMDb found no usable movie for "
                f'"{rating.title}{suffix}"; skipping row.'
            )
            continue
        tmdb_id = int(resolved["id"])
        rated_ids.add(tmdb_id)
        if rating.liked:
            weight = 1.5 if rating.rating == 5 else 1.0
            seed_weights[tmdb_id] = max(seed_weights.get(tmdb_id, 0.0), weight)

    candidates: dict[int, Recommendation] = {}
    for liked_id in sorted(seed_weights):
        seed_weight = seed_weights[liked_id]
        seen_for_seed: set[int] = set()
        for payload in client.get_movie_recommendations(liked_id):
            candidate = _candidate_from_payload(
                payload,
                today=today,
                weighted_support=seed_weight,
            )
            if (
                candidate is None
                or candidate.tmdb_id in rated_ids
                or candidate.tmdb_id in seen_for_seed
            ):
                continue
            seen_for_seed.add(candidate.tmdb_id)
            previous = candidates.get(candidate.tmdb_id)
            if previous is None:
                candidates[candidate.tmdb_id] = candidate
            else:
                candidates[candidate.tmdb_id] = Recommendation(
                    tmdb_id=candidate.tmdb_id,
                    title=candidate.title,
                    year=candidate.year,
                    weighted_support=previous.weighted_support + seed_weight,
                    genre_ids=candidate.genre_ids,
                    popularity=max(previous.popularity, candidate.popularity),
                    vote_average=max(previous.vote_average, candidate.vote_average),
                    vote_count=max(previous.vote_count, candidate.vote_count),
                )

    selected = _select_personalized(list(candidates.values()))
    discovery = _load_discovery_candidates(
        client,
        rated_ids=rated_ids,
        selected_ids={item.tmdb_id for item in selected},
        needed=RECOMMENDATION_COUNT - len(selected),
        today=today,
    )

    while len(selected) < RECOMMENDATION_COUNT - 1 and discovery:
        choice = _select_discovery_backfill(list(discovery.values()), selected)
        selected.append(choice)
        discovery.pop(choice.tmdb_id)

    if discovery and len(selected) < RECOMMENDATION_COUNT:
        exploration = _select_exploration(list(discovery.values()), selected)
        selected.append(exploration)

    return selected


def format_recommendations(recommendations: list[Recommendation]) -> str:
    """Format the plain-text recommendation file."""
    return "".join(f"{item.title} ({item.year})\n" for item in recommendations)


def _most_popular_valid_result(results: list[JsonObject]) -> JsonObject | None:
    valid = [item for item in results if _positive_int(item.get("id")) is not None]
    if not valid:
        return None
    return max(
        valid,
        key=lambda item: (
            _finite_number(item.get("popularity"), -1.0),
            -int(item["id"]),
        ),
    )


def _candidate_from_payload(
    payload: JsonObject,
    *,
    today: date,
    weighted_support: float = 0.0,
) -> Recommendation | None:
    tmdb_id = _positive_int(payload.get("id"))
    title = payload.get("title")
    release_text = payload.get("release_date")
    raw_genre_ids = payload.get("genre_ids")
    popularity = _finite_number(payload.get("popularity"))
    vote_average = _finite_number(payload.get("vote_average"))
    vote_count = _nonnegative_int(payload.get("vote_count"))
    if (
        tmdb_id is None
        or not isinstance(title, str)
        or not title.strip()
        or payload.get("adult") is not False
        or payload.get("video") is not False
        or not isinstance(release_text, str)
        or not isinstance(raw_genre_ids, list)
        or not raw_genre_ids
        or any(_positive_int(value) is None for value in raw_genre_ids)
        or popularity is None
        or vote_average is None
        or vote_count is None
    ):
        return None
    try:
        release_date = date.fromisoformat(release_text)
    except ValueError:
        return None
    if release_date > today:
        return None
    return Recommendation(
        tmdb_id=tmdb_id,
        title=title.strip(),
        year=release_date.year,
        weighted_support=weighted_support,
        genre_ids=frozenset(int(value) for value in raw_genre_ids),
        popularity=popularity,
        vote_average=vote_average,
        vote_count=vote_count,
    )


def _select_personalized(candidates: list[Recommendation]) -> list[Recommendation]:
    if not candidates:
        return []
    first = min(candidates, key=_relevance_key)
    remaining = [item for item in candidates if item.tmdb_id != first.tmdb_id]
    if not remaining:
        return [first]

    preferred = [item for item in remaining if _genre_overlap(item, first.genre_ids) <= 1]
    if preferred:
        second = min(preferred, key=_relevance_key)
    else:
        second = min(
            remaining,
            key=lambda item: (_genre_overlap(item, first.genre_ids), *_relevance_key(item)),
        )
    return [first, second]


def _load_discovery_candidates(
    client: RecommendationClient,
    *,
    rated_ids: set[int],
    selected_ids: set[int],
    needed: int,
    today: date,
) -> dict[int, Recommendation]:
    candidates: dict[int, Recommendation] = {}
    for page in range(1, DISCOVERY_MAX_PAGES + 1):
        payloads = client.discover_movies(
            page=page,
            released_through=today,
            min_vote_average=DISCOVERY_MIN_VOTE_AVERAGE,
            min_vote_count=DISCOVERY_MIN_VOTE_COUNT,
        )
        for payload in payloads:
            candidate = _candidate_from_payload(payload, today=today)
            if (
                candidate is not None
                and candidate.tmdb_id not in rated_ids
                and candidate.tmdb_id not in selected_ids
                and candidate.vote_average >= DISCOVERY_MIN_VOTE_AVERAGE
                and candidate.vote_count >= DISCOVERY_MIN_VOTE_COUNT
            ):
                candidates.setdefault(candidate.tmdb_id, candidate)
        if len(candidates) >= needed:
            break
    return candidates


def _select_discovery_backfill(
    candidates: list[Recommendation],
    selected: list[Recommendation],
) -> Recommendation:
    if not selected:
        return min(candidates, key=_quality_key)
    reference_genres = selected[0].genre_ids
    preferred = [item for item in candidates if _genre_overlap(item, reference_genres) <= 1]
    if preferred:
        return min(preferred, key=_quality_key)
    return min(
        candidates,
        key=lambda item: (_genre_overlap(item, reference_genres), *_quality_key(item)),
    )


def _select_exploration(
    candidates: list[Recommendation],
    selected: list[Recommendation],
) -> Recommendation:
    selected_genres = frozenset(
        genre_id for recommendation in selected for genre_id in recommendation.genre_ids
    )
    return min(
        candidates,
        key=lambda item: (_genre_overlap(item, selected_genres), *_quality_key(item)),
    )


def _relevance_key(item: Recommendation) -> tuple[float, float, float, int]:
    return (-item.weighted_support, -item.popularity, -item.vote_average, item.tmdb_id)


def _quality_key(item: Recommendation) -> tuple[float, int, float, int]:
    return (-item.vote_average, -item.vote_count, -item.popularity, item.tmdb_id)


def _genre_overlap(item: Recommendation, genre_ids: frozenset[int]) -> int:
    return len(item.genre_ids & genre_ids)


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _finite_number(value: object, default: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return default
