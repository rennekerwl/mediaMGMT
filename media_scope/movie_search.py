"""Movie-only Jackett search stage driven by recommendation JSON on standard input."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from media_scope.exceptions import (
    AllIndexersFailedError,
    JackettAuthenticationError,
    JackettConfigurationError,
    JackettError,
)
from media_scope.jackett_client import JackettClient, magnet_btih
from media_scope.models import JsonObject
from media_scope.release_classifier import normalize_release_title
from media_scope.search_models import IndexerCapabilities, RawRelease
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.movie_search")
JACKETT_RESULTS_FILENAME = "JACKETTRESULTS.txt"
RESULT_LIMIT = 10


class MovieSearchInputError(ValueError):
    """Raised when recommendation JSON is not a valid movie-search handoff."""


@dataclass(frozen=True, slots=True)
class MovieRecommendation:
    """Validated recommendation accepted from the preceding stage."""

    tmdb_id: int
    title: str
    year: int

    def to_dict(self) -> JsonObject:
        return {"tmdb_id": self.tmdb_id, "title": self.title, "year": self.year}


@dataclass(slots=True)
class _Candidate:
    dedup_key: str
    sources: list[RawRelease] = field(default_factory=list)
    starts_with_title: bool = False

    @property
    def seeders(self) -> int | None:
        return _maximum(source.seeders for source in self.sources)

    @property
    def peers(self) -> int | None:
        return _maximum(source.peers for source in self.sources)

    @property
    def published_at(self) -> str | None:
        values = [source.published_at for source in self.sources if source.published_at]
        return max(values) if values else None


class MovieSearchClient(Protocol):
    """Jackett operations used by the movie-search workflow."""

    def discover_indexers(self) -> list[IndexerCapabilities]: ...

    def get_capabilities(self, indexer_id: str) -> IndexerCapabilities: ...

    def search_movies(
        self,
        indexer: IndexerCapabilities,
        query: str,
        *,
        fresh: bool,
        sequence_start: int,
    ) -> list[RawRelease]: ...


class ClientContext(AbstractContextManager[MovieSearchClient], Protocol):
    """Context-managed movie-search client returned by the CLI factory."""


ClientFactory = Callable[[str, str], ClientContext]


def build_parser() -> argparse.ArgumentParser:
    """Build the movie-search command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m media_scope.movie_search",
        description="Read movie recommendations from stdin and search Jackett.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ask Jackett to bypass its Torznab result cache.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output JSON.")
    parser.add_argument("--verbose", action="store_true", help="Enable informational logging.")
    return parser


def load_recommendations(text: str) -> list[MovieRecommendation]:
    """Validate recommendation JSON received from standard input."""
    if not text.strip():
        raise MovieSearchInputError("Recommendation input was empty.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MovieSearchInputError("Recommendation input was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise MovieSearchInputError("Recommendation input must be a JSON object.")
    if payload.get("schema_version") != 1:
        raise MovieSearchInputError("Recommendation schema_version must be 1.")
    raw_values = payload.get("recommendations")
    if not isinstance(raw_values, list):
        raise MovieSearchInputError("Recommendation input must contain a recommendations list.")

    recommendations: list[MovieRecommendation] = []
    seen_ids: set[int] = set()
    for position, value in enumerate(raw_values, start=1):
        if not isinstance(value, dict):
            raise MovieSearchInputError(f"Recommendation {position} must be a JSON object.")
        tmdb_id = _positive_int(value.get("tmdb_id"))
        title = value.get("title")
        year = _year(value.get("year"))
        if tmdb_id is None:
            raise MovieSearchInputError(f"Recommendation {position} has an invalid tmdb_id.")
        if not isinstance(title, str) or not title.strip():
            raise MovieSearchInputError(f"Recommendation {position} has an invalid title.")
        if year is None:
            raise MovieSearchInputError(f"Recommendation {position} has an invalid year.")
        if tmdb_id in seen_ids:
            raise MovieSearchInputError(f"Recommendation {position} repeats tmdb_id {tmdb_id}.")
        seen_ids.add(tmdb_id)
        recommendations.append(MovieRecommendation(tmdb_id, title.strip(), year))
    return recommendations


def search_recommended_movies(
    client: MovieSearchClient,
    recommendations: Sequence[MovieRecommendation],
    *,
    indexer_ids: tuple[str, ...] = (),
    fresh: bool = False,
    searched_at: datetime | None = None,
) -> JsonObject:
    """Search, filter, deduplicate, rank, and serialize recommended movies."""
    timestamp = _utc_text(searched_at or datetime.now(UTC))
    if not recommendations:
        return _empty_report(timestamp)

    indexers, warnings = _resolve_indexers(client, indexer_ids)
    groups: list[JsonObject] = []
    successful_request_count = 0
    successful_indexers: set[str] = set()
    failed_indexers: set[str] = set()
    next_sequence = 0

    for recommendation in recommendations:
        query = f"{recommendation.title} {recommendation.year}"
        raw_results: list[RawRelease] = []
        failed_for_movie: list[str] = []
        for indexer in indexers:
            try:
                values = client.search_movies(
                    indexer,
                    query,
                    fresh=fresh,
                    sequence_start=next_sequence,
                )
            except JackettAuthenticationError:
                raise
            except JackettError as exc:
                failed_for_movie.append(indexer.id)
                failed_indexers.add(indexer.id)
                warnings.append(
                    _warning(
                        "INDEXER_QUERY_FAILED",
                        f"Indexer {indexer.id} failed while searching for {recommendation.title}.",
                        tmdb_id=recommendation.tmdb_id,
                        indexer_id=indexer.id,
                        error_code=exc.error_code,
                    )
                )
                continue
            successful_request_count += 1
            successful_indexers.add(indexer.id)
            raw_results.extend(values)
            next_sequence += len(values)

        if failed_for_movie and len(failed_for_movie) == len(indexers):
            warnings.append(
                _warning(
                    "MOVIE_SEARCH_FAILED",
                    f"Every indexer failed while searching for {recommendation.title}.",
                    tmdb_id=recommendation.tmdb_id,
                )
            )
        groups.append(_rank_movie_results(recommendation, query, raw_results))

    if successful_request_count == 0:
        raise AllIndexersFailedError(
            "Every movie-indexer request failed.",
            diagnostics=[dict(value) for value in warnings],
        )

    if failed_indexers:
        warnings.append(
            _warning(
                "PARTIAL_INDEXER_FAILURE",
                "At least one indexer query failed; successful movie results were retained.",
            )
        )
    return {
        "schema_version": 1,
        "result": "movie_search_completed",
        "searched_at": timestamp,
        "fresh": fresh,
        "indexers_requested": [indexer.id for indexer in indexers],
        "indexers_succeeded": sorted(successful_indexers),
        "indexers_failed": sorted(failed_indexers),
        "movies": groups,
        "warnings": warnings,
    }


def format_jackett_results(payload: JsonObject) -> str:
    """Render the safe, human-readable Jackett results artifact."""
    lines = ["JACKETT MOVIE RESULTS", f"Searched: {payload['searched_at']}", ""]
    movies = payload.get("movies")
    if not isinstance(movies, list) or not movies:
        lines.append("No recommendations to search.")
        return "\n".join(lines) + "\n"

    for movie in movies:
        if not isinstance(movie, dict):
            continue
        recommendation = movie.get("recommendation")
        if not isinstance(recommendation, dict):
            continue
        lines.extend(
            [
                f"{recommendation.get('title')} ({recommendation.get('year')})",
                f"Query: {movie.get('query')}",
                (
                    f"Raw: {movie.get('raw_result_count')} | "
                    f"Accepted: {movie.get('accepted_result_count')} | "
                    f"Returned: {movie.get('returned_result_count')}"
                ),
            ]
        )
        results = movie.get("results")
        if not isinstance(results, list) or not results:
            lines.append("No accepted results with reported seeders.")
            lines.append("")
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            source_names = result.get("source_indexers")
            sources = ", ".join(str(value) for value in source_names or [])
            lines.append(f"{result.get('rank')}. {result.get('title')}")
            lines.append(f"   Indexers: {sources or 'unknown'}")
            lines.append(
                f"   Seeders: {result.get('reported_seeders')} | "
                f"Peers: {_display(result.get('reported_peers'))} | "
                f"Size: {_format_size(result.get('size_bytes'))}"
            )
            lines.append(f"   Published: {_display(result.get('published_at'))}")
            lines.append(f"   Magnet: {_display(result.get('magnet_uri'))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    input_text: str | None = None,
    searched_at: datetime | None = None,
) -> int:
    """Run the stdin-to-stdout movie-search stage."""
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv()

    try:
        raw_input = input_text if input_text is not None else sys.stdin.read()
        recommendations = load_recommendations(raw_input)
    except (OSError, MovieSearchInputError) as exc:
        LOGGER.error("%s", exc)
        return 2

    directory_text = os.getenv("RECOMMENDATIONS_DIRECTORY", "").strip()
    if not directory_text:
        LOGGER.error(
            "RECOMMENDATIONS_DIRECTORY is missing. Configure it in the environment or .env file."
        )
        return 2
    output_directory = Path(directory_text).expanduser()
    if not output_directory.is_dir():
        LOGGER.error("RECOMMENDATIONS_DIRECTORY does not identify an accessible directory.")
        return 2

    try:
        if recommendations:
            url = os.getenv("JACKETT_URL", "").strip()
            api_key = os.getenv("JACKETT_API_KEY", "").strip()
            factory = client_factory or JackettClient
            with factory(url, api_key) as client:
                payload = search_recommended_movies(
                    client,
                    recommendations,
                    indexer_ids=_indexer_ids(os.getenv("JACKETT_INDEXERS", "")),
                    fresh=args.fresh,
                    searched_at=searched_at,
                )
        else:
            payload = _empty_report(_utc_text(searched_at or datetime.now(UTC)))
    except JackettConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2
    except JackettError as exc:
        LOGGER.error("Jackett movie search failed: %s", exc)
        return 4
    except Exception:
        LOGGER.exception("Unexpected movie-search failure.")
        return 1

    output = output_directory / JACKETT_RESULTS_FILENAME
    try:
        _write_atomic(output, format_jackett_results(payload))
    except OSError:
        LOGGER.exception("Could not write %s.", JACKETT_RESULTS_FILENAME)
        return 5

    sys.stdout.write(serialize_json(payload, pretty=args.pretty))
    LOGGER.info("Wrote Jackett movie results to %s.", output)
    return 0


def _resolve_indexers(
    client: MovieSearchClient,
    requested_ids: tuple[str, ...],
) -> tuple[list[IndexerCapabilities], list[JsonObject]]:
    warnings: list[JsonObject] = []
    capabilities: list[IndexerCapabilities] = []
    if requested_ids:
        for indexer_id in requested_ids:
            try:
                capabilities.append(client.get_capabilities(indexer_id))
            except JackettAuthenticationError:
                raise
            except JackettError as exc:
                warnings.append(
                    _warning(
                        "INDEXER_CAPABILITIES_FAILED",
                        f"Could not load capabilities for indexer {indexer_id}.",
                        indexer_id=indexer_id,
                        error_code=exc.error_code,
                    )
                )
    else:
        capabilities = client.discover_indexers()

    selected: list[IndexerCapabilities] = []
    for indexer in capabilities:
        if indexer.search_available and indexer.supports_movie_category:
            selected.append(indexer)
            continue
        warnings.append(
            _warning(
                "INDEXER_NOT_MOVIE_CAPABLE",
                f"Indexer {indexer.id} does not support generic movie-category searches.",
                indexer_id=indexer.id,
            )
        )
    if not selected:
        raise AllIndexersFailedError(
            "No configured indexer supports generic movie-category searches.",
            diagnostics=[dict(value) for value in warnings],
        )
    return selected, warnings


def _rank_movie_results(
    recommendation: MovieRecommendation,
    query: str,
    raw_results: Sequence[RawRelease],
) -> JsonObject:
    candidates: dict[str, _Candidate] = {}
    rejected = 0
    matched = 0
    for release in raw_results:
        match = _title_match(release.normalized_title, recommendation)
        if match is None or not _has_usable_reference(release):
            rejected += 1
            continue
        matched += 1
        dedup_key = _dedup_key(release)
        candidate = candidates.setdefault(dedup_key, _Candidate(dedup_key))
        candidate.sources.append(release)
        candidate.starts_with_title = candidate.starts_with_title or match

    duplicate_count = matched - len(candidates)
    eligible: list[_Candidate] = []
    for candidate in candidates.values():
        if candidate.seeders is None or candidate.seeders <= 0:
            rejected += 1
            continue
        eligible.append(candidate)
    eligible.sort(key=_ranking_key)
    selected = eligible[:RESULT_LIMIT]
    results = [_candidate_dict(candidate, rank) for rank, candidate in enumerate(selected, 1)]
    return {
        "recommendation": recommendation.to_dict(),
        "query": query,
        "raw_result_count": len(raw_results),
        "rejected_result_count": rejected,
        "duplicate_result_count": duplicate_count,
        "accepted_result_count": len(eligible),
        "returned_result_count": len(results),
        "results": results,
    }


def _candidate_dict(candidate: _Candidate, rank: int) -> JsonObject:
    representative = max(candidate.sources, key=_representative_key)
    magnet_source = next((source for source in candidate.sources if source.magnet_uri), None)
    download_source = next(
        (
            source
            for source in candidate.sources
            if source.download_url or _http_reference(source.guid)
        ),
        None,
    )
    infohash = next((source.infohash for source in candidate.sources if source.infohash), None)
    categories: dict[int, JsonObject] = {}
    for source in candidate.sources:
        for category in source.categories:
            categories.setdefault(category.id, category.to_dict())
    sources = sorted(
        candidate.sources,
        key=lambda value: (value.indexer_id.casefold(), value.sequence),
    )
    warnings = list(
        dict.fromkeys(warning for source in candidate.sources for warning in source.warnings)
    )
    return {
        "rank": rank,
        "title": representative.original_title,
        "match_quality": "title_starts" if candidate.starts_with_title else "title_contained",
        "published_at": candidate.published_at,
        "size_bytes": representative.size_bytes,
        "reported_seeders": candidate.seeders,
        "reported_peers": candidate.peers,
        "infohash": infohash,
        "magnet_uri": magnet_source.magnet_uri if magnet_source else None,
        "download_url": (
            download_source.download_url or download_source.guid if download_source else None
        ),
        "details_url": representative.details_url,
        "download_volume_factor": representative.download_volume_factor,
        "upload_volume_factor": representative.upload_volume_factor,
        "categories": [categories[key] for key in sorted(categories)],
        "source_indexers": list(dict.fromkeys(source.indexer_id for source in sources)),
        "sources": [_source_dict(source) for source in sources],
        "warnings": warnings,
    }


def _source_dict(source: RawRelease) -> JsonObject:
    return {
        "indexer_id": source.indexer_id,
        "indexer_name": source.indexer_name,
        "title": source.original_title,
        "published_at": source.published_at,
        "size_bytes": source.size_bytes,
        "reported_seeders": source.seeders,
        "reported_peers": source.peers,
        "download_url": source.download_url or _http_reference(source.guid),
        "details_url": source.details_url,
    }


def _title_match(normalized_release: str, recommendation: MovieRecommendation) -> bool | None:
    release_tokens = normalized_release.split()
    title_tokens = normalize_release_title(recommendation.title).split()
    if not title_tokens or str(recommendation.year) not in release_tokens:
        return None
    width = len(title_tokens)
    positions = [
        position
        for position in range(len(release_tokens) - width + 1)
        if release_tokens[position : position + width] == title_tokens
    ]
    if not positions:
        return None
    return 0 in positions


def _has_usable_reference(release: RawRelease) -> bool:
    return any(
        (
            release.infohash,
            release.magnet_uri,
            release.download_url,
            _http_reference(release.guid),
        )
    )


def _http_reference(value: str | None) -> str | None:
    if value and value.casefold().startswith(("http://", "https://")):
        return value
    return None


def _dedup_key(release: RawRelease) -> str:
    infohash = release.infohash or magnet_btih(release.magnet_uri)
    if infohash:
        return f"btih:{infohash}"
    if release.size_bytes is not None:
        return f"title-size:{release.normalized_title}:{release.size_bytes}"
    return f"occurrence:{release.indexer_id.casefold()}:{release.sequence}"


def _ranking_key(candidate: _Candidate) -> tuple[object, ...]:
    representative = max(candidate.sources, key=_representative_key)
    return (
        -int(candidate.starts_with_title),
        -(candidate.seeders or 0),
        -(candidate.peers if candidate.peers is not None else -1),
        -len({source.indexer_id for source in candidate.sources}),
        -_published_timestamp(candidate.published_at),
        representative.normalized_title,
        representative.size_bytes if representative.size_bytes is not None else -1,
        candidate.dedup_key,
    )


def _representative_key(release: RawRelease) -> tuple[int, int, int, int, int]:
    return (
        int(bool(release.magnet_uri)),
        int(bool(release.infohash)),
        release.seeders if release.seeders is not None else -1,
        release.peers if release.peers is not None else -1,
        -release.sequence,
    )


def _published_timestamp(value: str | None) -> float:
    if value is None:
        return -1.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return -1.0


def _maximum(values: Any) -> int | None:
    present = [value for value in values if isinstance(value, int)]
    return max(present) if present else None


def _indexer_ids(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _empty_report(searched_at: str) -> JsonObject:
    return {
        "schema_version": 1,
        "result": "movie_search_completed",
        "searched_at": searched_at,
        "fresh": False,
        "indexers_requested": [],
        "indexers_succeeded": [],
        "indexers_failed": [],
        "movies": [],
        "warnings": [],
    }


def _warning(code: str, message: str, **context: Any) -> JsonObject:
    return {"code": code, "message": message, **context}


def _write_atomic(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _format_size(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "unknown"
    if value < 1024:
        return f"{value} B"
    units = ("KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        size /= 1024
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
    return f"{value} B"


def _display(value: object) -> str:
    return "unknown" if value is None else str(value)


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _year(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 9999:
        return value
    return None


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
