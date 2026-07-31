"""Complete-series Jackett search orchestration, deduplication, and ranking."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from media_scope.exceptions import (
    AllIndexersFailedError,
    JackettAuthenticationError,
    JackettError,
)
from media_scope.jackett_client import JackettClient, magnet_btih
from media_scope.magnet_resolver import (
    MagnetResolution,
    MagnetResolver,
)
from media_scope.models import JsonObject
from media_scope.release_classifier import (
    classify_release,
    with_expected_seasons,
    with_merged_release,
)
from media_scope.search_models import (
    CanonicalResult,
    ClassificationEvidence,
    IndexerCapabilities,
    RawRelease,
    ReleaseClassification,
    SearchScope,
    TorznabCategory,
    json_warning,
)
from media_scope.search_query_builder import build_search_queries

LOGGER = logging.getLogger("media_scope.search")
_CLASS_PRIORITY = {
    ReleaseClassification.COMPLETE_SERIES: 9,
    ReleaseClassification.SINGLE_SEASON_PACK: 8,
    ReleaseClassification.COMPLETE_SERIES_CANDIDATE: 7,
    ReleaseClassification.MULTI_SEASON_PACK: 6,
    ReleaseClassification.SINGLE_EPISODE: 5,
    ReleaseClassification.MULTI_EPISODE_PARTIAL: 4,
    ReleaseClassification.ANIME: 3,
    ReleaseClassification.MOVIE_OR_UNRELATED: 2,
    ReleaseClassification.UNKNOWN: 1,
}


def search_complete_series(
    client: JackettClient,
    scope: SearchScope,
    *,
    indexer_ids: tuple[str, ...] = (),
    fresh: bool = False,
    min_seeders: int = 0,
    include_rejected: bool = False,
    max_rejected: int = 25,
    magnet_resolver: MagnetResolver | None = None,
) -> JsonObject:
    """Search, classify, deduplicate, rank, and serialize complete-series results."""
    queries = build_search_queries(scope)
    LOGGER.info(
        "Loaded scope for %s with %s expected seasons.",
        scope.title,
        len(scope.expected_seasons),
    )
    LOGGER.info("Generated %s deterministic search queries.", len(queries))

    capabilities, capability_failures = _resolve_indexers(client, indexer_ids)
    requested_ids = list(indexer_ids) if indexer_ids else [item.id for item in capabilities]
    LOGGER.info("Querying %s Jackett indexers.", len(requested_ids))

    raw_results: list[RawRelease] = []
    requests: list[JsonObject] = []
    diagnostics: list[JsonObject] = list(capability_failures)
    succeeded: list[str] = []
    failed_ids = {str(value["indexer_id"]) for value in capability_failures}
    next_sequence = 0

    for indexer in capabilities:
        mode = indexer.search_mode
        if mode is None:
            diagnostics.append(
                {
                    "indexer_id": indexer.id,
                    "indexer_name": indexer.name,
                    "status": "failed",
                    "error_code": "UNSUPPORTED_SEARCH_MODE",
                    "message": "Indexer supports neither tvsearch nor generic search.",
                }
            )
            failed_ids.add(indexer.id)
            continue

        successful_requests = 0
        result_count = 0
        query_failures = 0
        for query in queries:
            request_diagnostic: JsonObject = {
                "indexer_id": indexer.id,
                "query": query,
                "mode": mode,
                "categories": [5000] if indexer.supports_tv_category else [],
                "fresh": fresh,
            }
            try:
                values = client.search(
                    indexer,
                    query,
                    fresh=fresh,
                    sequence_start=next_sequence,
                )
            except JackettAuthenticationError:
                raise
            except JackettError as exc:
                query_failures += 1
                request_diagnostic.update(
                    {
                        "status": "failed",
                        "error_code": exc.error_code,
                        "message": str(exc),
                        "raw_result_count": 0,
                    }
                )
            else:
                successful_requests += 1
                result_count += len(values)
                raw_results.extend(values)
                next_sequence += len(values)
                request_diagnostic.update(
                    {
                        "status": "succeeded",
                        "raw_result_count": len(values),
                    }
                )
            requests.append(request_diagnostic)

        if successful_requests:
            succeeded.append(indexer.id)
            status = "partial" if query_failures else "succeeded"
            diagnostics.append(
                {
                    "indexer_id": indexer.id,
                    "indexer_name": indexer.name,
                    "status": status,
                    "successful_query_count": successful_requests,
                    "failed_query_count": query_failures,
                    "raw_result_count": result_count,
                }
            )
            LOGGER.info("Indexer %s returned %s raw results.", indexer.id, result_count)
        else:
            failed_ids.add(indexer.id)
            diagnostics.append(
                {
                    "indexer_id": indexer.id,
                    "indexer_name": indexer.name,
                    "status": "failed",
                    "error_code": "ALL_QUERIES_FAILED",
                    "message": "Every query for this indexer failed.",
                }
            )
            LOGGER.warning("Every query failed for indexer %s.", indexer.id)

    if not succeeded:
        raise AllIndexersFailedError(
            "Every selected Jackett indexer failed.",
            diagnostics=diagnostics,
        )

    canonical = deduplicate_and_classify(raw_results, scope)
    rank_results(canonical, min_seeders=min_seeders)
    provisional = sorted(
        (value for value in canonical if value.evidence.accepted_as_candidate),
        key=_ranking_key,
    )
    resolver = magnet_resolver or MagnetResolver(client)
    resolution_succeeded = 0
    resolution_failed = 0
    for value in provisional:
        resolution = resolver.resolve(value)
        if isinstance(resolution, MagnetResolution):
            value.release = replace(
                value.release,
                infohash=resolution.infohash,
                magnet_uri=resolution.magnet_uri,
                magnet_source=resolution.source,
            )
            resolution_succeeded += 1
            continue
        resolution_failed += 1
        reasons = [*value.evidence.rejection_reasons, "NO_USABLE_MAGNET"]
        if resolution.code != "NO_USABLE_MAGNET":
            reasons.append(resolution.code)
        value.evidence = replace(
            value.evidence,
            accepted_as_candidate=False,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(
                dict.fromkeys(
                    [
                        *value.evidence.warnings,
                        f"{resolution.code}: {resolution.message}",
                        *(
                            f"MAGNET_RESOLUTION_DIAGNOSTIC: {diagnostic}"
                            for diagnostic in resolution.diagnostics
                        ),
                    ]
                )
            ),
        )

    rank_results(canonical, min_seeders=min_seeders)
    accepted = sorted(
        (value for value in canonical if value.evidence.accepted_as_candidate),
        key=_ranking_key,
    )
    rejected = sorted(
        (value for value in canonical if not value.evidence.accepted_as_candidate),
        key=lambda value: (value.release.sequence, value.release.normalized_title),
    )
    for rank, value in enumerate(accepted, start=1):
        value.rank = rank

    classification_counts = Counter(value.evidence.classification.value for value in canonical)
    rejection_counts = Counter(value.evidence.classification.value for value in rejected)
    warnings: list[JsonObject] = [*scope.warnings]
    if failed_ids or any(item.get("status") == "partial" for item in diagnostics):
        warnings.append(
            json_warning(
                "PARTIAL_INDEXER_FAILURE",
                "At least one indexer or query failed; successful results were retained.",
            )
        )
    if not raw_results:
        warnings.append(json_warning("NO_SEARCH_RESULTS", "Jackett returned no search results."))
    elif provisional and not accepted:
        warnings.append(
            json_warning(
                "NO_USABLE_MAGNET_CANDIDATES",
                "Search results were found, but none produced a usable magnet URI.",
            )
        )
    elif not accepted:
        warnings.append(
            json_warning(
                "NO_COMPLETE_SERIES_CANDIDATES",
                "Search results were found, but none met complete-series candidate rules.",
            )
        )

    rejected_details = _serialize_rejected(
        rejected,
        scope,
        include_rejected=include_rejected,
        limit=max_rejected,
    )
    LOGGER.info(
        "Search produced %s raw, %s deduplicated, and %s accepted results.",
        len(raw_results),
        len(canonical),
        len(accepted),
    )
    return {
        "schema_version": 1,
        "result": "search_completed",
        "media_type": "tv",
        "scope": scope.to_dict(),
        "search": {
            "queries": list(queries),
            "requests": requests,
            "indexers_requested": requested_ids,
            "indexers_succeeded": succeeded,
            "indexers_failed": sorted(failed_ids),
            "indexer_diagnostics": diagnostics,
            "raw_result_count": len(raw_results),
            "deduplicated_result_count": len(canonical),
            "provisional_candidate_count": len(provisional),
            "magnet_resolution_attempted_count": len(provisional),
            "magnet_resolution_succeeded_count": resolution_succeeded,
            "magnet_resolution_failed_count": resolution_failed,
            "accepted_candidate_count": len(accepted),
            "classification_counts": dict(sorted(classification_counts.items())),
            "rejection_counts": dict(sorted(rejection_counts.items())),
            "rejected_result_count": len(rejected),
            "rejected_details_count": len(rejected_details),
            "rejected_omitted_count": max(len(rejected) - len(rejected_details), 0),
            "min_seeders_for_ranking": min_seeders,
        },
        "candidates": [
            with_expected_seasons(value.to_dict(), scope.expected_seasons) for value in accepted
        ],
        "rejected_results": rejected_details,
        "warnings": warnings,
    }


def deduplicate_and_classify(
    releases: list[RawRelease],
    scope: SearchScope,
) -> list[CanonicalResult]:
    """Deduplicate occurrences conservatively and classify the canonical records."""
    classified = {value.sequence: classify_release(value, scope) for value in releases}
    groups: dict[str, list[RawRelease]] = {}
    for release in releases:
        key = _dedup_key(release)
        groups.setdefault(key, []).append(release)

    results: list[CanonicalResult] = []
    for key, sources in groups.items():
        sources.sort(key=lambda value: value.sequence)
        representative = max(
            sources,
            key=lambda value: _representative_key(value, classified[value.sequence]),
        )
        categories = _merge_categories(sources)
        seeders = _maximum(value.seeders for value in sources)
        peers = _maximum(value.peers for value in sources)
        infohash = representative.infohash or next(
            (value.infohash for value in sources if value.infohash),
            None,
        )
        magnet = representative.magnet_uri or next(
            (value.magnet_uri for value in sources if value.magnet_uri),
            None,
        )
        size = representative.size_bytes
        if size is None:
            size = next(
                (value.size_bytes for value in sources if value.size_bytes is not None),
                None,
            )
        merged_release = with_merged_release(
            representative,
            categories=categories,
            seeders=seeders,
            peers=peers,
            infohash=infohash,
            magnet_uri=magnet,
            size_bytes=size,
        )
        evidence = classify_release(merged_release, scope)
        conflicts = _metadata_conflicts(sources)
        results.append(
            CanonicalResult(
                release=merged_release,
                evidence=evidence,
                sources=sources,
                categories=categories,
                dedup_key=key,
                warnings=conflicts,
            )
        )
    return sorted(results, key=lambda value: value.release.sequence)


def rank_results(results: list[CanonicalResult], *, min_seeders: int) -> None:
    """Assign deterministic correctness-first scores to accepted candidates."""
    for result in results:
        if not result.evidence.accepted_as_candidate:
            result.score = None
            result.score_reasons = []
            continue
        adjustments: list[tuple[int, str]] = []

        evidence = result.evidence
        release = result.release
        if evidence.title_match:
            adjustments.append((25, "Exact normalized show-title prefix match"))
        if evidence.exact_coverage:
            adjustments.append((30, "Detected season coverage exactly matches expected seasons"))
        if evidence.explicit_range:
            adjustments.append((15, "Explicit season range detected"))
        if evidence.complete_series_phrase:
            adjustments.append((15, "Explicit complete-series phrase"))
        if evidence.all_seasons_phrase:
            adjustments.append((12, "Explicit all-seasons phrase"))
        if evidence.year_match:
            adjustments.append((5, "First-air year matches"))
        if evidence.tv_category:
            adjustments.append((5, "Recognized television category"))
        if release.infohash:
            adjustments.append((4, "Infohash present"))
        if release.magnet_uri:
            adjustments.append((3, "Magnet URI present"))
        if release.size_bytes and release.size_bytes > 0:
            adjustments.append((3, "Nonzero size present"))
        else:
            adjustments.append((-3, "Size is missing or zero"))
        if release.seeders is None:
            adjustments.append((-2, "Seeder count is missing"))
        else:
            adjustments.append(
                (min(8, int(math.log2(release.seeders + 1))), "Bounded seeder support")
            )
            if min_seeders > 0 and release.seeders < min_seeders:
                denominator = max(release.seeders, 1)
                penalty = min(10, max(1, math.ceil(math.log2(min_seeders / denominator))))
                adjustments.append((-penalty, f"Seeders are below ranking threshold {min_seeders}"))
        if evidence.bare_complete:
            adjustments.append((-10, "Ambiguous bare Complete"))
        if evidence.category_missing:
            adjustments.append((-3, "Category information is missing"))
        if evidence.extra_seasons:
            penalty = min(15, 5 + 2 * (len(evidence.extra_seasons) - 1))
            adjustments.append((-penalty, "Unexplained extra seasons"))
        if evidence.conflicting_seasons:
            adjustments.append((-15, "Conflicting season evidence"))

        score = sum(points for points, _reason in adjustments)
        result.score = max(0, min(100, score))
        result.score_reasons = [f"{points:+d} {reason}" for points, reason in adjustments]


def _resolve_indexers(
    client: JackettClient,
    indexer_ids: tuple[str, ...],
) -> tuple[list[IndexerCapabilities], list[JsonObject]]:
    if not indexer_ids:
        return client.discover_indexers(), []
    capabilities: list[IndexerCapabilities] = []
    failures: list[JsonObject] = []
    for indexer_id in indexer_ids:
        try:
            capabilities.append(client.get_capabilities(indexer_id))
        except JackettAuthenticationError:
            raise
        except JackettError as exc:
            failures.append(
                {
                    "indexer_id": indexer_id,
                    "indexer_name": indexer_id,
                    "status": "failed",
                    "error_code": exc.error_code,
                    "message": str(exc),
                }
            )
    if not capabilities:
        raise AllIndexersFailedError(
            "No explicitly selected indexer returned valid capabilities.",
            diagnostics=failures,
        )
    return capabilities, failures


def _dedup_key(release: RawRelease) -> str:
    if release.infohash:
        return f"infohash:{release.infohash}"
    btih = magnet_btih(release.magnet_uri)
    if btih:
        return f"btih:{btih}"
    if release.guid:
        return f"guid:{release.indexer_id.casefold()}:{release.guid}"
    if release.size_bytes and release.published_at:
        return f"composite:{release.normalized_title}:{release.size_bytes}:{release.published_at}"
    return f"occurrence:{release.sequence}"


def _representative_key(
    release: RawRelease,
    evidence: ClassificationEvidence,
) -> tuple[int, int, int, int]:
    return (
        int(evidence.accepted_as_candidate),
        _CLASS_PRIORITY[evidence.classification],
        int(evidence.exact_coverage),
        -release.sequence,
    )


def _merge_categories(releases: list[RawRelease]) -> tuple[TorznabCategory, ...]:
    values: dict[int, TorznabCategory] = {}
    for release in releases:
        for category in release.categories:
            current = values.get(category.id)
            if current is None or (current.name is None and category.name is not None):
                values[category.id] = category
    return tuple(sorted(values.values(), key=lambda category: category.id))


def _maximum(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if isinstance(value, int)]
    return max(present) if present else None


def _metadata_conflicts(releases: list[RawRelease]) -> list[str]:
    warnings: list[str] = []
    checks = (
        ("normalized_title", "Duplicate sources reported different normalized titles."),
        ("size_bytes", "Duplicate sources reported conflicting sizes."),
        ("infohash", "Duplicate sources reported conflicting infohashes."),
        ("published_at", "Duplicate sources reported conflicting publication timestamps."),
        ("peers", "Duplicate sources reported conflicting peer counts."),
    )
    for field_name, message in checks:
        values = {
            getattr(release, field_name)
            for release in releases
            if getattr(release, field_name) is not None
        }
        if len(values) > 1:
            warnings.append(message)
    return warnings


def _ranking_key(value: CanonicalResult) -> tuple[object, ...]:
    confidence = {"high": 3, "medium": 2, "low": 1}.get(value.evidence.confidence, 0)
    return (
        -(value.score or 0),
        -confidence,
        -int(value.evidence.exact_coverage),
        -(value.release.seeders if value.release.seeders is not None else -1),
        value.release.normalized_title,
        value.release.size_bytes if value.release.size_bytes is not None else -1,
        value.dedup_key,
        value.release.sequence,
    )


def _serialize_rejected(
    rejected: list[CanonicalResult],
    scope: SearchScope,
    *,
    include_rejected: bool,
    limit: int,
) -> list[JsonObject]:
    selected = rejected[:limit]
    if include_rejected:
        return [
            with_expected_seasons(value.to_dict(), scope.expected_seasons) for value in selected
        ]
    return [
        {
            "original_title": value.release.original_title,
            "classification": value.evidence.classification.value,
            "accepted_as_candidate": False,
            "content_verified": False,
            "rejection_reasons": list(value.evidence.rejection_reasons),
            "sources": [
                {
                    "indexer_id": source.indexer_id,
                    "indexer_name": source.indexer_name,
                    "query": source.query,
                }
                for source in value.sources
            ],
        }
        for value in selected
    ]
