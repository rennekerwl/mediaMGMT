"""Typed models used by deterministic complete-series searches."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from media_scope.models import JsonObject


class ReleaseClassification(StrEnum):
    """Primary release-title classifications emitted by the search component."""

    COMPLETE_SERIES = "COMPLETE_SERIES"
    COMPLETE_SERIES_CANDIDATE = "COMPLETE_SERIES_CANDIDATE"
    MULTI_SEASON_PACK = "MULTI_SEASON_PACK"
    SINGLE_SEASON_PACK = "SINGLE_SEASON_PACK"
    SINGLE_EPISODE = "SINGLE_EPISODE"
    MULTI_EPISODE_PARTIAL = "MULTI_EPISODE_PARTIAL"
    MOVIE_OR_UNRELATED = "MOVIE_OR_UNRELATED"
    ANIME = "ANIME"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SearchScope:
    """Validated complete-ended-series scope consumed by the search service."""

    tmdb_id: int
    title: str
    original_title: str | None
    first_air_year: int | None
    status: str
    expected_seasons: tuple[int, ...]
    warnings: tuple[JsonObject, ...] = ()

    def to_dict(self) -> JsonObject:
        """Return the public scope summary."""
        return {
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "original_title": self.original_title,
            "first_air_year": self.first_air_year,
            "status": self.status,
            "expected_seasons": list(self.expected_seasons),
            "expected_season_count": len(self.expected_seasons),
        }


@dataclass(frozen=True, slots=True, order=True)
class TorznabCategory:
    """One normalized Torznab category."""

    id: int
    name: str | None = None

    def to_dict(self) -> JsonObject:
        """Return the public category representation."""
        value: JsonObject = {"id": self.id}
        if self.name:
            value["name"] = self.name
        return value


@dataclass(frozen=True, slots=True)
class IndexerCapabilities:
    """Capabilities and category mappings advertised by one Jackett indexer."""

    id: str
    name: str
    tvsearch_available: bool
    search_available: bool
    categories: tuple[TorznabCategory, ...]

    @property
    def search_mode(self) -> str | None:
        """Return the preferred supported search mode."""
        if self.tvsearch_available:
            return "tvsearch"
        if self.search_available:
            return "search"
        return None

    @property
    def supports_tv_category(self) -> bool:
        """Return whether standard Torznab TV category 5000 is advertised."""
        return any(category.id == 5000 for category in self.categories)

    def category_name(self, category_id: int) -> str | None:
        """Resolve an advertised category name."""
        return next(
            (category.name for category in self.categories if category.id == category_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class RawRelease:
    """One occurrence returned by one indexer for one query."""

    sequence: int
    indexer_id: str
    indexer_name: str
    query: str
    original_title: str
    normalized_title: str
    guid: str | None = None
    download_url: str | None = None
    details_url: str | None = None
    published_at: str | None = None
    categories: tuple[TorznabCategory, ...] = ()
    size_bytes: int | None = None
    seeders: int | None = None
    peers: int | None = None
    infohash: str | None = None
    magnet_uri: str | None = None
    magnet_source: str | None = None
    download_volume_factor: float | None = None
    upload_volume_factor: float | None = None
    warnings: tuple[str, ...] = ()
    torznab_magnet_uri: str | None = field(default=None, repr=False, compare=False)
    result_field_magnets: tuple[str, ...] = field(default=(), repr=False, compare=False)
    internal_download_references: tuple[str, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

    @property
    def leechers(self) -> int | None:
        """Derive leechers when both peers and seeders are known."""
        if self.peers is None or self.seeders is None:
            return None
        return max(self.peers - self.seeders, 0)

    @property
    def has_usable_reference(self) -> bool:
        """Return whether a later component can identify or retrieve the result."""
        return any(
            (
                self.infohash,
                self.magnet_uri,
                self.download_url,
                self.guid,
                self.internal_download_references,
            )
        )

    def source_dict(self) -> JsonObject:
        """Return the public source-occurrence representation."""
        value: JsonObject = {
            "indexer_id": self.indexer_id,
            "indexer_name": self.indexer_name,
            "query": self.query,
            "guid": self.guid,
            "download_url": self.download_url,
            "details_url": self.details_url,
            "published_at": self.published_at,
            "seeders": self.seeders,
        }
        return value


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    """Deterministic classification and the evidence used to reach it."""

    classification: ReleaseClassification
    accepted_as_candidate: bool
    confidence: str
    detected_seasons: tuple[int, ...]
    detected_episodes: tuple[str, ...]
    missing_seasons: tuple[int, ...]
    extra_seasons: tuple[int, ...]
    matched_patterns: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    title_match: bool = False
    year_match: bool = False
    exact_coverage: bool = False
    explicit_range: bool = False
    complete_series_phrase: bool = False
    all_seasons_phrase: bool = False
    bare_complete: bool = False
    tv_category: bool = False
    category_missing: bool = False
    conflicting_seasons: bool = False


@dataclass(slots=True)
class CanonicalResult:
    """Deduplicated result with merged sources and ranking metadata."""

    release: RawRelease
    evidence: ClassificationEvidence
    sources: list[RawRelease]
    categories: tuple[TorznabCategory, ...]
    dedup_key: str
    warnings: list[str] = field(default_factory=list)
    score: int | None = None
    score_reasons: list[str] = field(default_factory=list)
    rank: int | None = None

    def to_dict(self) -> JsonObject:
        """Return the complete public result representation."""
        release = self.release
        evidence = self.evidence
        warnings = list(dict.fromkeys([*evidence.warnings, *release.warnings, *self.warnings]))
        return {
            "rank": self.rank,
            "classification": evidence.classification.value,
            "accepted_as_candidate": evidence.accepted_as_candidate,
            "confidence": evidence.confidence,
            "content_verified": False,
            "original_title": release.original_title,
            "normalized_title": release.normalized_title,
            "detected_seasons": list(evidence.detected_seasons),
            "detected_episodes": list(evidence.detected_episodes),
            "expected_seasons": None,
            "missing_seasons": list(evidence.missing_seasons),
            "extra_seasons": list(evidence.extra_seasons),
            "size_bytes": release.size_bytes,
            "published_at": release.published_at,
            "seeders": release.seeders,
            "leechers": release.leechers,
            "peers": release.peers,
            "infohash": release.infohash,
            "magnet_uri": release.magnet_uri,
            "magnet_source": release.magnet_source,
            "download_url": release.download_url,
            "details_url": release.details_url,
            "download_volume_factor": release.download_volume_factor,
            "upload_volume_factor": release.upload_volume_factor,
            "categories": [category.to_dict() for category in self.categories],
            "source_indexers": list(dict.fromkeys(source.indexer_id for source in self.sources)),
            "sources": [source.source_dict() for source in self.sources],
            "score": self.score,
            "score_reasons": self.score_reasons,
            "matched_patterns": list(evidence.matched_patterns),
            "rejection_reasons": list(evidence.rejection_reasons),
            "warnings": warnings,
        }


def json_warning(code: str, message: str, **context: Any) -> JsonObject:
    """Build a stable public warning object."""
    return {"code": code, "message": message, **context}
