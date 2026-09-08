"""Taste-aware movie recommendation collection and selection.

This module deliberately keeps the model-facing records separate from the legacy
weighted recommendation records.  The model receives one record per eligible
movie and the final result is reconstructed from those trusted records after the
selector responds.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from media_scope.exceptions import TmdbError
from media_scope.models import JsonObject
from media_scope.openrouter_client import OpenRouterError
from media_scope.recommendations import (
    DISCOVERY_MAX_PAGES,
    DISCOVERY_MIN_VOTE_AVERAGE,
    DISCOVERY_MIN_VOTE_COUNT,
    RECOMMENDATION_COUNT,
    RatingRow,
    Recommendation,
    RecommendationClient,
    WarningHandler,
    _candidate_from_payload,
    _most_popular_valid_result,
    _positive_int,
)

TMDB_GENRE_NAMES = {
    12: "Adventure",
    14: "Fantasy",
    16: "Animation",
    18: "Drama",
    27: "Horror",
    28: "Action",
    35: "Comedy",
    36: "History",
    37: "Western",
    53: "Thriller",
    80: "Crime",
    99: "Documentary",
    878: "Science Fiction",
    9648: "Mystery",
    10402: "Music",
    10749: "Romance",
    10751: "Family",
    10752: "War",
    10770: "TV Movie",
}


class LlmRecommendationClient(RecommendationClient, Protocol):
    """TMDb operations required by the taste-aware recommendation workflow."""

    def get_movie(self, tmdb_id: int) -> JsonObject: ...


class SelectionClient(Protocol):
    """Selector interface implemented by the OpenRouter client."""

    def select(
        self,
        *,
        candidates: list[JsonObject],
        taste_history: list[JsonObject],
        count: int,
    ) -> list[JsonObject]: ...


@dataclass(frozen=True)
class SelectionResult:
    """Recommendations plus diagnostics from one model or fallback selection."""

    recommendations: list[Recommendation]
    reasons: dict[int, str]
    method: str
    candidate_count: int


@dataclass(frozen=True)
class CandidatePool:
    """Model candidates and their private source lists for deterministic fallback."""

    candidates: list[JsonObject]
    seed_lists: tuple[tuple[int, tuple[JsonObject, ...]], ...]
    discovery: tuple[JsonObject, ...]


@dataclass(frozen=True)
class ResolvedRating:
    """A valid rating paired with the resolved TMDb ID when one exists."""

    rating: RatingRow
    tmdb_id: int | None


def build_llm_recommendations(
    client: LlmRecommendationClient,
    ratings: list[RatingRow],
    *,
    selector: SelectionClient,
    today: date,
    warn: WarningHandler,
    excluded_ids: Collection[int] = (),
) -> SelectionResult:
    """Collect a complete recommendation context and select up to three movies.

    TMDb records are collected once per source ID and rated history is enriched
    once per distinct ID.  Selection responses are independently checked and
    mapped back to the collected records before they become ``Recommendation``
    values.
    """
    details_cache: dict[int, JsonObject | None] = {}
    resolved = resolve_ratings(client, ratings, warn=warn)
    history = build_taste_history(
        client,
        resolved,
        details_cache=details_cache,
    )

    rated_ids = {item.tmdb_id for item in resolved if item.tmdb_id is not None}
    excluded = {_positive_int(value) for value in excluded_ids}
    excluded.discard(None)
    rated_ids.update(excluded)
    seed_ratings: dict[int, int] = {}
    seed_ids: list[int] = []
    for item in resolved:
        if item.tmdb_id is None or not item.rating.liked:
            continue
        seed_ratings[item.tmdb_id] = max(seed_ratings.get(item.tmdb_id, 0), item.rating.rating)
        if item.tmdb_id not in seed_ids:
            seed_ids.append(item.tmdb_id)

    pool = collect_candidates(
        client,
        seed_ids=seed_ids,
        rated_ids=rated_ids,
        today=today,
    )
    candidate_count = len(pool.candidates)
    count = min(RECOMMENDATION_COUNT, candidate_count)
    if count == 0:
        return SelectionResult([], {}, "fallback", candidate_count)

    try:
        selected = selector.select(
            candidates=pool.candidates,
            taste_history=history,
            count=count,
        )
        selected_records, reasons = validate_selection(
            selected,
            candidates=pool.candidates,
            count=count,
        )
        return SelectionResult(
            recommendations=[_recommendation_from_metadata(item) for item in selected_records],
            reasons=reasons,
            method="llm",
            candidate_count=candidate_count,
        )
    except OpenRouterError as exc:
        warn(f"LLM recommendation selection failed; using deterministic fallback: {exc}")
    except InvalidSelectionError as exc:
        warn(f"LLM recommendation selection was invalid; using deterministic fallback: {exc}")

    fallback_records = fallback_selection(pool, seed_ratings=seed_ratings, count=count)
    fallback_reasons = _fallback_reasons(
        fallback_records,
        pool=pool,
        seed_ratings=seed_ratings,
    )
    return SelectionResult(
        recommendations=[_recommendation_from_metadata(item) for item in fallback_records],
        reasons=fallback_reasons,
        method="fallback",
        candidate_count=candidate_count,
    )


def resolve_ratings(
    client: RecommendationClient,
    ratings: Sequence[RatingRow],
    *,
    warn: WarningHandler,
) -> list[ResolvedRating]:
    """Resolve every valid rating using the same popularity rule as legacy mode."""
    resolved: list[ResolvedRating] = []
    for rating in ratings:
        tmdb_id = rating.tmdb_id if _positive_int(rating.tmdb_id) is not None else None
        if tmdb_id is None:
            results = client.search_movies(rating.title, rating.year)
            match = _most_popular_valid_result(results)
            if match is not None:
                tmdb_id = int(match["id"])
            else:
                suffix = f" ({rating.year})" if rating.year is not None else ""
                warn(
                    f"Sheet row {rating.row_number}: TMDb found no usable movie for "
                    f'"{rating.title}{suffix}"; keeping rating as unresolved context.'
                )
        resolved.append(ResolvedRating(rating=rating, tmdb_id=tmdb_id))
    return resolved


def build_taste_history(
    client: LlmRecommendationClient,
    ratings: Sequence[ResolvedRating] | Sequence[RatingRow],
    *,
    details_cache: dict[int, JsonObject | None] | None = None,
) -> list[JsonObject]:
    """Build context for every valid rating, including dislikes and Notes.

    The function accepts either already-resolved ratings or ``RatingRow`` values
    for convenient isolated tests.  Callers using raw rows should resolve them
    first when they need IDs in the resulting history.
    """
    cache = details_cache if details_cache is not None else {}
    history: list[JsonObject] = []
    for value in ratings:
        if isinstance(value, ResolvedRating):
            resolved = value
        else:
            resolved = ResolvedRating(
                value,
                value.tmdb_id if _positive_int(value.tmdb_id) else None,
            )
        record: JsonObject = {
            "title": resolved.rating.title,
            "year": resolved.rating.year,
            "rating": resolved.rating.rating,
            "notes": resolved.rating.notes,
        }
        if resolved.tmdb_id is not None:
            record["tmdb_id"] = resolved.tmdb_id
            details = _movie_details(client, resolved.tmdb_id, cache)
            _merge_details(record, details)
        history.append(record)
    return history


def collect_candidates(
    client: LlmRecommendationClient,
    *,
    seed_ids: Sequence[int],
    rated_ids: Collection[int],
    today: date,
) -> CandidatePool:
    """Collect eligible page-one seed recommendations and three discovery pages."""
    excluded = {item for item in rated_ids if _positive_int(item) is not None}
    candidates_by_id: dict[int, JsonObject] = {}
    seed_lists: dict[int, list[JsonObject]] = {seed_id: [] for seed_id in seed_ids}

    for seed_id in seed_ids:
        payloads = client.get_movie_recommendations(seed_id)
        if not isinstance(payloads, list):
            continue
        source_seen: set[int] = set()
        for payload in payloads:
            candidate = _model_candidate(payload, today=today)
            if candidate is None:
                continue
            tmdb_id = int(candidate["tmdb_id"])
            if tmdb_id in excluded or tmdb_id in source_seen:
                continue
            source_seen.add(tmdb_id)
            if tmdb_id in candidates_by_id:
                candidate = candidates_by_id[tmdb_id]
            else:
                candidates_by_id[tmdb_id] = candidate
            seed_lists[seed_id].append(candidate)

    discovery: list[JsonObject] = []
    discovery_seen: set[int] = set()
    for page in range(1, DISCOVERY_MAX_PAGES + 1):
        payloads = client.discover_movies(
            page=page,
            released_through=today,
            min_vote_average=DISCOVERY_MIN_VOTE_AVERAGE,
            min_vote_count=DISCOVERY_MIN_VOTE_COUNT,
        )
        if not payloads:
            break
        if not isinstance(payloads, list):
            continue
        for payload in payloads:
            candidate = _model_candidate(payload, today=today)
            if candidate is None:
                continue
            tmdb_id = int(candidate["tmdb_id"])
            if candidate["vote_average"] < DISCOVERY_MIN_VOTE_AVERAGE:
                continue
            if candidate["vote_count"] < DISCOVERY_MIN_VOTE_COUNT:
                continue
            if tmdb_id in excluded or tmdb_id in discovery_seen:
                continue
            discovery_seen.add(tmdb_id)
            if tmdb_id in candidates_by_id:
                candidate = candidates_by_id[tmdb_id]
            else:
                candidates_by_id[tmdb_id] = candidate
            discovery.append(candidate)

    return CandidatePool(
        candidates=list(candidates_by_id.values()),
        seed_lists=tuple((seed_id, tuple(seed_lists[seed_id])) for seed_id in seed_ids),
        discovery=tuple(discovery),
    )


def validate_selection(
    selected: object,
    *,
    candidates: Sequence[JsonObject],
    count: int,
) -> tuple[list[JsonObject], dict[int, str]]:
    """Validate selector output and return trusted candidate records plus reasons."""
    if not isinstance(selected, list):
        raise InvalidSelectionError("selection must be a list")
    if len(selected) != count:
        raise InvalidSelectionError(f"expected {count} selections, received {len(selected)}")
    by_id: dict[int, JsonObject] = {}
    for candidate in candidates:
        tmdb_id = _positive_int(candidate.get("tmdb_id"))
        if tmdb_id is not None:
            by_id[tmdb_id] = candidate

    trusted: list[JsonObject] = []
    reasons: dict[int, str] = {}
    for item in selected:
        if not isinstance(item, dict):
            raise InvalidSelectionError("selection item must be an object")
        tmdb_id = _positive_int(item.get("tmdb_id"))
        reason = item.get("reason")
        if tmdb_id is None or tmdb_id not in by_id:
            raise InvalidSelectionError("selection contains an unknown TMDb ID")
        if tmdb_id in reasons:
            raise InvalidSelectionError("selection contains duplicate TMDb IDs")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidSelectionError("each selection requires a non-empty reason")
        reasons[tmdb_id] = reason.strip()
        trusted.append(by_id[tmdb_id])
    return trusted, reasons


def fallback_selection(
    pool: CandidatePool,
    *,
    seed_ratings: dict[int, int],
    count: int,
) -> list[JsonObject]:
    """Select by seed-list round robin, with discovery as the final source each turn."""
    ordered_seeds = sorted(
        pool.seed_lists,
        key=lambda pair: (-seed_ratings.get(pair[0], 0), pair[0]),
    )
    selected: list[JsonObject] = []
    selected_ids: set[int] = set()
    seed_positions = [0 for _ in ordered_seeds]
    discovery_position = 0
    while len(selected) < count:
        made_progress = False
        for source_index, (_seed_id, items) in enumerate(ordered_seeds):
            while seed_positions[source_index] < len(items):
                candidate = items[seed_positions[source_index]]
                seed_positions[source_index] += 1
                tmdb_id = int(candidate["tmdb_id"])
                if tmdb_id in selected_ids:
                    continue
                selected_ids.add(tmdb_id)
                selected.append(candidate)
                made_progress = True
                break
            if len(selected) == count:
                return selected

        while discovery_position < len(pool.discovery):
            candidate = pool.discovery[discovery_position]
            discovery_position += 1
            tmdb_id = int(candidate["tmdb_id"])
            if tmdb_id in selected_ids:
                continue
            selected_ids.add(tmdb_id)
            selected.append(candidate)
            made_progress = True
            break
        if not made_progress:
            break
    return selected


class InvalidSelectionError(ValueError):
    """Raised when a selector returns an unsafe or incomplete selection."""


def _model_candidate(
    payload: JsonObject,
    *,
    today: date,
) -> JsonObject | None:
    base = _candidate_from_payload(payload, today=today)
    if base is None:
        return None
    tmdb_id = base.tmdb_id
    record: JsonObject = {
        "tmdb_id": tmdb_id,
        "title": base.title,
        "year": base.year,
        "overview": base.overview or "",
        "genres": [
            TMDB_GENRE_NAMES.get(int(value), str(int(value))) for value in sorted(base.genre_ids)
        ],
        "genre_ids": [int(value) for value in sorted(base.genre_ids)],
        "popularity": base.popularity,
        "vote_average": base.vote_average,
        "vote_count": base.vote_count,
    }
    return record


def _movie_details(
    client: LlmRecommendationClient,
    tmdb_id: int,
    cache: dict[int, JsonObject | None],
) -> JsonObject | None:
    if tmdb_id in cache:
        return cache[tmdb_id]
    try:
        details = client.get_movie(tmdb_id)
    except TmdbError:
        details = None
    except (TypeError, ValueError, AttributeError):
        details = None
    if not isinstance(details, dict):
        details = None
    cache[tmdb_id] = details
    return details


def _merge_details(record: JsonObject, details: JsonObject | None) -> None:
    """Merge safe enrichment fields while retaining every basic field."""
    if details is None:
        return
    overview = details.get("overview")
    if isinstance(overview, str) and overview.strip():
        record["overview"] = overview.strip()

    genres = details.get("genres")
    if isinstance(genres, list):
        names: list[str] = []
        for genre in genres:
            if isinstance(genre, dict) and isinstance(genre.get("name"), str):
                name = genre["name"].strip()
                if name:
                    names.append(name)
        if names:
            record["genres"] = names


def _fallback_reasons(
    records: Sequence[JsonObject],
    *,
    pool: CandidatePool,
    seed_ratings: dict[int, int],
) -> dict[int, str]:
    """Explain each fallback pick using its first ordered source."""
    ordered_seeds = sorted(
        pool.seed_lists,
        key=lambda pair: (-seed_ratings.get(pair[0], 0), pair[0]),
    )
    source_by_id: dict[int, str] = {}
    for seed_id, items in ordered_seeds:
        for item in items:
            tmdb_id = int(item["tmdb_id"])
            source_by_id.setdefault(
                tmdb_id,
                f"TMDb recommendations for rated seed TMDb ID {seed_id}.",
            )
    for item in pool.discovery:
        source_by_id.setdefault(int(item["tmdb_id"]), "TMDb discovery.")
    return {
        int(item["tmdb_id"]): source_by_id.get(
            int(item["tmdb_id"]), "Selected by deterministic fallback."
        )
        for item in records
    }


def _recommendation_from_metadata(candidate: JsonObject) -> Recommendation:
    genres = candidate.get("genre_ids", candidate.get("genres"))
    genre_ids = frozenset(
        value for value in (genres or []) if isinstance(value, int) and not isinstance(value, bool)
    )
    return Recommendation(
        tmdb_id=int(candidate["tmdb_id"]),
        title=str(candidate["title"]),
        year=int(candidate["year"]),
        weighted_support=0.0,
        genre_ids=genre_ids,
        popularity=float(candidate["popularity"]),
        vote_average=float(candidate["vote_average"]),
        vote_count=int(candidate["vote_count"]),
        overview=candidate.get("overview") if isinstance(candidate.get("overview"), str) else None,
    )
