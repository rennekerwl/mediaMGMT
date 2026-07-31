"""Deduplication, ranking, and per-indexer orchestration tests."""

from __future__ import annotations

from typing import Any

import pytest

from media_scope.exceptions import AllIndexersFailedError, JackettNetworkError
from media_scope.release_classifier import normalize_release_title
from media_scope.search_models import (
    IndexerCapabilities,
    RawRelease,
    SearchScope,
    TorznabCategory,
)
from media_scope.search_service import (
    deduplicate_and_classify,
    rank_results,
    search_complete_series,
)

SCOPE = SearchScope(1, "Example Show", None, 2017, "Ended", (1, 2, 3, 4, 5))
TV = (TorznabCategory(5000, "TV"),)


def raw(
    sequence: int,
    title: str,
    *,
    indexer: str = "alpha",
    query: str = "query",
    infohash: str | None = None,
    guid: str | None = None,
    size: int | None = 1000,
    published: str | None = "2020-01-01T00:00:00Z",
    seeders: int | None = 1,
) -> RawRelease:
    return RawRelease(
        sequence=sequence,
        indexer_id=indexer,
        indexer_name=indexer.title(),
        query=query,
        original_title=title,
        normalized_title=normalize_release_title(title),
        guid=guid,
        published_at=published,
        categories=TV,
        size_bytes=size,
        seeders=seeders,
        infohash=infohash,
        magnet_uri=(f"magnet:?xt=urn:btih:{infohash}" if infohash else None),
    )


def test_same_infohash_merges_queries_and_indexers_preserving_sources() -> None:
    infohash = "a" * 40
    values = deduplicate_and_classify(
        [
            raw(0, "Example.Show.S01-S05", infohash=infohash, seeders=10),
            raw(
                1,
                "Example Show Complete Series",
                indexer="beta",
                query="other",
                infohash=infohash,
                seeders=30,
            ),
        ],
        SCOPE,
    )
    assert len(values) == 1
    assert len(values[0].sources) == 2
    assert values[0].release.seeders == 30
    assert {source.indexer_id for source in values[0].sources} == {"alpha", "beta"}


def test_identical_titles_with_different_hashes_remain_separate() -> None:
    values = deduplicate_and_classify(
        [
            raw(0, "Example Show Complete Series", infohash="a" * 40),
            raw(1, "Example Show Complete Series", infohash="b" * 40),
        ],
        SCOPE,
    )
    assert len(values) == 2


def test_guid_is_indexer_scoped_and_composite_fallback_is_conservative() -> None:
    guid_values = deduplicate_and_classify(
        [
            raw(0, "Example Show Complete Series", indexer="alpha", guid="same"),
            raw(1, "Example Show Complete Series", indexer="beta", guid="same"),
        ],
        SCOPE,
    )
    assert len(guid_values) == 2

    composite = deduplicate_and_classify(
        [
            raw(0, "Example Show Complete Series", query="one"),
            raw(1, "Example.Show.Complete.Series", query="two"),
        ],
        SCOPE,
    )
    assert len(composite) == 1

    missing_publication = deduplicate_and_classify(
        [
            raw(0, "Example Show Complete Series", published=None),
            raw(1, "Example Show Complete Series", published=None),
        ],
        SCOPE,
    )
    assert len(missing_publication) == 2


def test_exact_coverage_outranks_vague_complete_and_incomplete_is_rejected() -> None:
    values = deduplicate_and_classify(
        [
            raw(0, "Example Show S01-S05", guid="exact", seeders=2),
            raw(1, "Example Show Complete Series", guid="vague", seeders=200),
            raw(2, "Example Show S01-S04", guid="partial", seeders=5000),
        ],
        SCOPE,
    )
    rank_results(values, min_seeders=0)
    accepted = sorted(
        (value for value in values if value.evidence.accepted_as_candidate),
        key=lambda value: -(value.score or 0),
    )
    assert accepted[0].release.guid == "exact"
    partial = next(value for value in values if value.release.guid == "partial")
    assert not partial.evidence.accepted_as_candidate
    assert partial.score is None


def test_min_seeders_changes_score_but_not_acceptance() -> None:
    value = deduplicate_and_classify(
        [raw(0, "Example Show S01-S05", guid="exact", seeders=0)],
        SCOPE,
    )[0]
    rank_results([value], min_seeders=10)
    assert value.evidence.accepted_as_candidate
    assert any("below ranking threshold" in reason for reason in value.score_reasons)


def test_ranking_is_stable_for_ties() -> None:
    values = deduplicate_and_classify(
        [
            raw(0, "Example Show S01-S05 1080p", guid="b"),
            raw(1, "Example Show S01-S05 720p", guid="a"),
        ],
        SCOPE,
    )
    rank_results(values, min_seeders=0)
    ordered_once = sorted(
        values,
        key=lambda value: (
            -(value.score or 0),
            value.release.normalized_title,
            value.dedup_key,
        ),
    )
    ordered_twice = sorted(
        values,
        key=lambda value: (
            -(value.score or 0),
            value.release.normalized_title,
            value.dedup_key,
        ),
    )
    assert [value.dedup_key for value in ordered_once] == [
        value.dedup_key for value in ordered_twice
    ]


class ServiceClient:
    """Small service double with configurable per-indexer failures."""

    def __init__(self, *, fail: set[str] | None = None, empty: bool = False) -> None:
        self.fail = fail or set()
        self.empty = empty
        self.calls: list[tuple[str, str, bool]] = []

    def discover_indexers(self) -> list[IndexerCapabilities]:
        return [
            IndexerCapabilities("alpha", "Alpha", True, True, TV),
            IndexerCapabilities("beta", "Beta", False, True, TV),
        ]

    def get_capabilities(self, indexer_id: str) -> IndexerCapabilities:
        return IndexerCapabilities(indexer_id, indexer_id.title(), True, True, TV)

    def search(
        self,
        indexer: IndexerCapabilities,
        query: str,
        *,
        fresh: bool,
        sequence_start: int,
    ) -> list[RawRelease]:
        self.calls.append((indexer.id, query, fresh))
        if indexer.id in self.fail:
            raise JackettNetworkError("temporary failure")
        if self.empty or query != "Example Show Complete Series":
            return []
        return [
            raw(
                sequence_start,
                "Example Show S01-S05",
                indexer=indexer.id,
                infohash="a" * 40,
            )
        ]


def test_partial_indexer_failure_completes_with_diagnostics() -> None:
    client = ServiceClient(fail={"beta"})
    payload = search_complete_series(client, SCOPE, fresh=True)  # type: ignore[arg-type]
    assert payload["result"] == "search_completed"
    assert payload["search"]["indexers_succeeded"] == ["alpha"]
    assert payload["search"]["indexers_failed"] == ["beta"]
    assert payload["search"]["accepted_candidate_count"] == 1
    assert payload["candidates"][0]["rank"] == 1
    assert all(fresh for _indexer, _query, fresh in client.calls)


def test_no_results_is_completed_search_and_all_failures_raise() -> None:
    payload = search_complete_series(ServiceClient(empty=True), SCOPE)  # type: ignore[arg-type]
    assert payload["search"]["raw_result_count"] == 0
    assert payload["candidates"] == []
    assert any(value["code"] == "NO_SEARCH_RESULTS" for value in payload["warnings"])

    with pytest.raises(AllIndexersFailedError):
        search_complete_series(  # type: ignore[arg-type]
            ServiceClient(fail={"alpha", "beta"}),
            SCOPE,
        )


def test_rejected_serialization_is_bounded_and_can_be_full() -> None:
    class RejectedClient(ServiceClient):
        def search(self, *args: Any, **kwargs: Any) -> list[RawRelease]:
            indexer = args[0]
            query = args[1]
            sequence_start = kwargs["sequence_start"]
            if query != "Example Show Complete Series":
                return []
            return [
                raw(
                    sequence_start + number,
                    f"Example Show S01E{number + 1:02d}",
                    indexer=indexer.id,
                    guid=f"g{number}",
                )
                for number in range(3)
            ]

    compact = search_complete_series(  # type: ignore[arg-type]
        RejectedClient(),
        SCOPE,
        max_rejected=1,
    )
    assert len(compact["rejected_results"]) == 1
    assert compact["search"]["rejected_omitted_count"] == 5
    assert "normalized_title" not in compact["rejected_results"][0]

    full = search_complete_series(  # type: ignore[arg-type]
        RejectedClient(),
        SCOPE,
        include_rejected=True,
        max_rejected=1,
    )
    assert "normalized_title" in full["rejected_results"][0]
