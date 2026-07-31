"""Safe title and TMDb-ID resolution."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from media_scope.client import TmdbClient
from media_scope.exceptions import AmbiguityError, InvalidResponseError, NotFoundError
from media_scope.models import JsonObject

MediaType = Literal["movie", "tv"]
_APOSTROPHES = {"'", "’", "‘", "ʼ", "`"}


def normalize_title(value: str) -> str:
    """Normalize case, punctuation, and repeated whitespace for exact comparison."""
    normalized = unicodedata.normalize("NFKC", value).lower().strip()
    characters: list[str] = []
    for character in normalized:
        if character in _APOSTROPHES:
            continue
        if unicodedata.category(character).startswith("P"):
            characters.append(" ")
        else:
            characters.append(character)
    return re.sub(r"\s+", " ", "".join(characters)).strip()


def resolve_media(
    client: TmdbClient,
    media_type: MediaType,
    *,
    title: str | None = None,
    year: int | None = None,
    tmdb_id: int | None = None,
) -> JsonObject:
    """Resolve one movie or television record without silently guessing."""
    if tmdb_id is not None:
        return client.get_movie(tmdb_id) if media_type == "movie" else client.get_tv(tmdb_id)
    if title is None or not title.strip():
        raise ValueError("title is required when tmdb_id is not supplied")

    results = (
        client.search_movies(title, year)
        if media_type == "movie"
        else client.search_tv(title, year)
    )
    if not results:
        raise NotFoundError(f'TMDb returned no {media_type} results for "{title}".')

    prepared = _prepare_candidates(results, media_type, title, year)
    exact = [item for item in prepared if item["exact_title"] and item["year_matches"]]
    unique_exact = {int(item["candidate"]["tmdb_id"]): item for item in exact}
    if len(unique_exact) == 1:
        selected_id = next(iter(unique_exact))
        return (
            client.get_movie(selected_id) if media_type == "movie" else client.get_tv(selected_id)
        )

    candidates = [item["candidate"] for item in prepared[:10]]
    raise AmbiguityError(
        media_type=media_type,
        query=title,
        year=year,
        candidates=candidates,
    )


def _prepare_candidates(
    results: list[JsonObject],
    media_type: MediaType,
    query: str,
    year: int | None,
) -> list[dict[str, Any]]:
    query_key = normalize_title(query)
    prepared: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, result in enumerate(results):
        tmdb_id = result.get("id")
        if not isinstance(tmdb_id, int):
            continue
        if tmdb_id in seen_ids:
            continue
        seen_ids.add(tmdb_id)

        title_key = "title" if media_type == "movie" else "name"
        original_key = "original_title" if media_type == "movie" else "original_name"
        date_key = "release_date" if media_type == "movie" else "first_air_date"
        year_key = "release_year" if media_type == "movie" else "first_air_year"
        display_value = result.get(title_key)
        original_value = result.get(original_key)
        display_title = display_value if isinstance(display_value, str) else None
        original_title = original_value if isinstance(original_value, str) else None
        if display_title is None and original_title is None:
            continue
        result_year = _year_from_date(result.get(date_key))
        normalized_titles = {
            normalize_title(candidate_title)
            for candidate_title in (display_title, original_title)
            if candidate_title is not None
        }
        exact_title = query_key in normalized_titles
        year_matches = year is None or result_year == year
        candidate: JsonObject = {
            "tmdb_id": tmdb_id,
            "title": display_title,
            "original_title": original_title,
            year_key: result_year,
            "overview": result.get("overview") if isinstance(result.get("overview"), str) else None,
        }
        prepared.append(
            {
                "candidate": candidate,
                "exact_title": exact_title,
                "year_matches": year_matches,
                "index": index,
            }
        )

    if not prepared:
        raise InvalidResponseError("TMDb search results contained no valid candidate records.")
    prepared.sort(
        key=lambda item: (
            not item["exact_title"],
            not item["year_matches"],
            item["index"],
            item["candidate"]["tmdb_id"],
        )
    )
    return prepared


def _year_from_date(value: object) -> int | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None
