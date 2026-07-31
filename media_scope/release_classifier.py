"""Deterministic release-title normalization and classification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from media_scope.search_models import (
    ClassificationEvidence,
    RawRelease,
    ReleaseClassification,
    SearchScope,
    TorznabCategory,
)

_SEPARATORS = re.compile(r"[\s._/\\:+|]+")
_PUNCTUATION = re.compile(r"[^\w\s-]", re.UNICODE)
_S_EPISODE = re.compile(r"(?i)\bS(\d{1,3})E(\d{1,3}(?:E\d{1,3})*)\b")
_X_EPISODE = re.compile(r"(?i)\b(\d{1,3})x(\d{1,3})\b")
_WORD_EPISODE = re.compile(r"(?i)\b(?:episode|ep)\s*0*(\d{1,4})\b")
_S_RANGE = re.compile(r"(?i)\bS(\d{1,3})\s*[-–—]\s*S?(\d{1,3})\b")
_TEXT_RANGE = re.compile(r"(?i)\bseasons?\s+(\d{1,3})\s*[-–—]\s*(\d{1,3})\b")
_S_TOKEN = re.compile(r"(?i)\bS(\d{1,3})\b")
_SEASON_NUMBER = re.compile(r"(?i)\bseason\s+(\d{1,3})\b")
_SEASON_LIST = re.compile(r"(?i)\bseasons?\s+((?:\d{1,3}\s*(?:(?:,|&|and)\s*)?){2,})")
_STRONG_COMPLETE = re.compile(
    r"(?i)\b(?:the\s+)?complete\s+(?:series|collection)\b|\bfull\s+series\b"
)
_ALL_SEASONS = re.compile(r"(?i)\ball(?:\s+(\d+))?\s+seasons?\b")
_BARE_COMPLETE = re.compile(r"(?i)\bcomplete\b")
_PACK = re.compile(r"(?i)\bpack\b")
_KNOWN_ANIME_GROUP = re.compile(
    r"(?i)^\s*\[(?:subsplease|erai[- ]?raws|horriblesubs|commie|judas)\]"
)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")

_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}
_ORDINAL_SEASON = re.compile(rf"(?i)\bcomplete\s+({'|'.join(_ORDINALS)})\s+season\b")

_BOUNDARY_TOKENS = {
    "complete",
    "the",
    "all",
    "full",
    "season",
    "seasons",
    "series",
    "collection",
    "pack",
    "episode",
    "ep",
    "480p",
    "720p",
    "1080p",
    "2160p",
    "4k",
    "web",
    "webdl",
    "webrip",
    "bluray",
    "bdrip",
    "hdtv",
    "dvdrip",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "av1",
    "aac",
    "ac3",
    "dts",
    "remux",
    "hdr",
    "dv",
    "proper",
    "repack",
}
_INAPPROPRIATE_CATEGORY_PREFIXES = {
    "console",
    "movies",
    "audio",
    "pc",
    "xxx",
    "books",
    "games",
    "software",
    "music",
    "adult",
    "sports",
}


def normalize_release_title(value: str) -> str:
    """Normalize release punctuation and spacing without removing semantic tokens."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = _SEPARATORS.sub(" ", normalized)
    normalized = re.sub(r"\s*[-–—]+\s*", " ", normalized)
    normalized = _PUNCTUATION.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def classify_release(release: RawRelease, scope: SearchScope) -> ClassificationEvidence:
    """Classify one release using only its title and Torznab metadata."""
    original = release.original_title
    normalized = release.normalized_title
    expected = set(scope.expected_seasons)
    warnings: list[str] = []
    matched: list[str] = []
    rejection: list[str] = []

    category_anime, category_inappropriate, tv_category, category_missing = _category_evidence(
        release
    )
    if category_anime or _KNOWN_ANIME_GROUP.search(original):
        reason = (
            "Torznab category identifies anime."
            if category_anime
            else "A conservative anime release-group signal was detected."
        )
        return _evidence(
            ReleaseClassification.ANIME,
            accepted=False,
            confidence="high",
            rejection=(reason,),
            tv_category=tv_category,
            category_missing=category_missing,
        )

    title_match, uncertain_title, year_match, title_warnings = _match_show_title(normalized, scope)
    warnings.extend(title_warnings)
    if not title_match:
        classification = (
            ReleaseClassification.UNKNOWN
            if uncertain_title
            else ReleaseClassification.MOVIE_OR_UNRELATED
        )
        reason = (
            "Release title contains the show title away from the expected leading position."
            if uncertain_title
            else "Release title does not match the intended show."
        )
        return _evidence(
            classification,
            accepted=False,
            confidence="medium" if uncertain_title else "high",
            rejection=(reason,),
            warnings=tuple(warnings),
            tv_category=tv_category,
            category_missing=category_missing,
        )
    if category_inappropriate:
        return _evidence(
            ReleaseClassification.MOVIE_OR_UNRELATED,
            accepted=False,
            confidence="high",
            rejection=("Torznab category contradicts standard television.",),
            warnings=tuple(warnings),
            title_match=True,
            year_match=year_match,
            tv_category=tv_category,
            category_missing=category_missing,
        )

    episode_tokens = _episode_tokens(original)
    if episode_tokens:
        matched.extend(episode_tokens)
        multiple = len(episode_tokens) > 1
        classification = (
            ReleaseClassification.MULTI_EPISODE_PARTIAL
            if multiple
            else ReleaseClassification.SINGLE_EPISODE
        )
        return _evidence(
            classification,
            accepted=False,
            confidence="high",
            detected_episodes=tuple(episode_tokens),
            matched=tuple(matched),
            rejection=(
                (
                    "Multiple individual episode tokens were detected."
                    if multiple
                    else f"Individual episode token detected: {episode_tokens[0]}"
                ),
            ),
            warnings=tuple(warnings),
            title_match=True,
            year_match=year_match,
            tv_category=tv_category,
            category_missing=category_missing,
        )

    season_sets, season_patterns, explicit_range = _season_evidence(original)
    matched.extend(season_patterns)
    detected = set().union(*season_sets) if season_sets else set()
    conflicting = len({tuple(sorted(value)) for value in season_sets}) > 1
    if conflicting:
        warnings.append("Conflicting season tokens were detected in the release title.")
    missing = expected - detected if detected else set()
    extra = detected - expected
    if extra:
        warnings.append(f"Unexplained extra regular seasons detected: {sorted(extra)}.")

    strong_complete = bool(_STRONG_COMPLETE.search(original))
    all_match = _ALL_SEASONS.search(original)
    all_seasons = bool(all_match)
    all_count_conflict = bool(
        all_match and all_match.group(1) and int(all_match.group(1)) != len(scope.expected_seasons)
    )
    if all_count_conflict:
        conflicting = True
        warnings.append("The stated all-seasons count does not match the expected season count.")
    bare_complete = bool(_BARE_COMPLETE.search(original)) and not strong_complete
    if strong_complete:
        matched.append(_STRONG_COMPLETE.search(original).group(0))  # type: ignore[union-attr]
    if all_seasons:
        matched.append(_ALL_SEASONS.search(original).group(0))  # type: ignore[union-attr]
    elif bare_complete:
        matched.append("Complete")

    single_pack = _is_single_season_pack(original, season_sets, detected)
    exact_coverage = bool(detected) and detected == expected
    accepted = False
    confidence = "medium"

    if single_pack:
        classification = ReleaseClassification.SINGLE_SEASON_PACK
        if len(expected) == 1 and exact_coverage and release.has_usable_reference:
            accepted = True
            confidence = "high"
        else:
            rejection.append("A single-season pack does not cover the complete expected series.")
    elif detected:
        if missing:
            classification = (
                ReleaseClassification.COMPLETE_SERIES_CANDIDATE
                if strong_complete or all_seasons
                else ReleaseClassification.MULTI_SEASON_PACK
            )
            rejection.append(f"Known missing regular seasons: {sorted(missing)}.")
            confidence = "high"
        elif exact_coverage and not conflicting:
            classification = ReleaseClassification.COMPLETE_SERIES
            accepted = release.has_usable_reference
            confidence = "high"
        else:
            classification = ReleaseClassification.COMPLETE_SERIES_CANDIDATE
            accepted = release.has_usable_reference and not conflicting
            confidence = "medium"
    elif strong_complete or all_seasons:
        if all_count_conflict:
            classification = ReleaseClassification.COMPLETE_SERIES_CANDIDATE
            rejection.append("The stated all-seasons count conflicts with the expected scope.")
            confidence = "high"
        else:
            classification = ReleaseClassification.COMPLETE_SERIES
            accepted = release.has_usable_reference
            confidence = "high" if strong_complete else "medium"
    elif bare_complete:
        classification = ReleaseClassification.COMPLETE_SERIES_CANDIDATE
        rejection.append("Bare Complete does not prove complete-series coverage.")
        confidence = "low"
    else:
        classification = ReleaseClassification.UNKNOWN
        rejection.append("No complete-series or season-coverage evidence was detected.")
        confidence = "low"

    if accepted and category_missing:
        warnings.append("No Torznab category information was supplied.")
    if (
        not release.has_usable_reference
        and classification
        in {
            ReleaseClassification.COMPLETE_SERIES,
            ReleaseClassification.COMPLETE_SERIES_CANDIDATE,
            ReleaseClassification.SINGLE_SEASON_PACK,
        }
        and not rejection
    ):
        rejection.append("No usable magnet, infohash, URL, or stable result reference is present.")
    if not accepted and not rejection:
        rejection.append("Title-only evidence is insufficient for candidate acceptance.")

    return _evidence(
        classification,
        accepted=accepted,
        confidence=confidence,
        detected_seasons=tuple(sorted(detected)),
        missing=tuple(sorted(missing)),
        extra=tuple(sorted(extra)),
        matched=tuple(dict.fromkeys(matched)),
        rejection=tuple(rejection),
        warnings=tuple(dict.fromkeys(warnings)),
        title_match=True,
        year_match=year_match,
        exact_coverage=exact_coverage,
        explicit_range=explicit_range,
        complete_series_phrase=strong_complete,
        all_seasons_phrase=all_seasons,
        bare_complete=bare_complete,
        tv_category=tv_category,
        category_missing=category_missing,
        conflicting=conflicting,
    )


def with_expected_seasons(
    value: dict[str, object], expected_seasons: tuple[int, ...]
) -> dict[str, object]:
    """Fill the expected-season field in a serialized result."""
    return {**value, "expected_seasons": list(expected_seasons)}


def with_merged_release(
    representative: RawRelease,
    *,
    categories: tuple[TorznabCategory, ...],
    seeders: int | None,
    peers: int | None,
    infohash: str | None,
    magnet_uri: str | None,
    size_bytes: int | None,
) -> RawRelease:
    """Create the release view used to classify merged occurrences."""
    return replace(
        representative,
        categories=categories,
        seeders=seeders,
        peers=peers,
        infohash=infohash,
        magnet_uri=magnet_uri,
        size_bytes=size_bytes,
    )


def _evidence(
    classification: ReleaseClassification,
    *,
    accepted: bool,
    confidence: str,
    detected_seasons: tuple[int, ...] = (),
    detected_episodes: tuple[str, ...] = (),
    missing: tuple[int, ...] = (),
    extra: tuple[int, ...] = (),
    matched: tuple[str, ...] = (),
    rejection: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    title_match: bool = False,
    year_match: bool = False,
    exact_coverage: bool = False,
    explicit_range: bool = False,
    complete_series_phrase: bool = False,
    all_seasons_phrase: bool = False,
    bare_complete: bool = False,
    tv_category: bool = False,
    category_missing: bool = False,
    conflicting: bool = False,
) -> ClassificationEvidence:
    return ClassificationEvidence(
        classification=classification,
        accepted_as_candidate=accepted,
        confidence=confidence,
        detected_seasons=detected_seasons,
        detected_episodes=detected_episodes,
        missing_seasons=missing,
        extra_seasons=extra,
        matched_patterns=matched,
        rejection_reasons=rejection,
        warnings=warnings,
        title_match=title_match,
        year_match=year_match,
        exact_coverage=exact_coverage,
        explicit_range=explicit_range,
        complete_series_phrase=complete_series_phrase,
        all_seasons_phrase=all_seasons_phrase,
        bare_complete=bare_complete,
        tv_category=tv_category,
        category_missing=category_missing,
        conflicting_seasons=conflicting,
    )


def _match_show_title(
    release_title: str,
    scope: SearchScope,
) -> tuple[bool, bool, bool, tuple[str, ...]]:
    aliases = [scope.title]
    if scope.original_title:
        aliases.append(scope.original_title)
    normalized_aliases = sorted(
        {normalize_release_title(value) for value in aliases},
        key=lambda value: (-len(value.split()), value),
    )
    uncertain = False
    warnings: list[str] = []
    for alias in normalized_aliases:
        if release_title == alias:
            return True, False, False, ()
        if not release_title.startswith(f"{alias} "):
            if f" {alias} " in f" {release_title} ":
                uncertain = True
            continue
        suffix = release_title[len(alias) + 1 :]
        first = suffix.split(" ", 1)[0]
        boundary = (
            first in _BOUNDARY_TOKENS
            or re.fullmatch(r"s\d{1,3}(?:e\d{1,3})*", first) is not None
            or re.fullmatch(r"\d{1,3}x\d{1,3}", first) is not None
            or re.fullmatch(r"(?:19|20|21)\d{2}", first) is not None
        )
        if not boundary:
            continue
        years = [int(value) for value in _YEAR.findall(suffix)]
        year_match = scope.first_air_year is not None and scope.first_air_year in years
        if years and scope.first_air_year is not None and not year_match:
            warnings.append("A release-title year does not match the first-air year.")
        return True, False, year_match, tuple(warnings)
    return False, uncertain, False, tuple(warnings)


def _category_evidence(release: RawRelease) -> tuple[bool, bool, bool, bool]:
    if not release.categories:
        return False, False, False, True
    anime = False
    inappropriate = False
    television = False
    for category in release.categories:
        name = (category.name or "").casefold()
        if category.id == 5070 or "anime" in name:
            anime = True
        if category.id == 5060 or "sport" in name:
            inappropriate = True
        parent = category.id // 1000
        if parent in {1, 2, 3, 4, 6, 7}:
            inappropriate = True
        if any(name.startswith(prefix) for prefix in _INAPPROPRIATE_CATEGORY_PREFIXES):
            inappropriate = True
        if parent == 5 and category.id not in {5060, 5070}:
            television = True
    missing = not anime and not inappropriate and not television
    return anime, inappropriate, television, missing


def _episode_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _S_EPISODE.finditer(value):
        season = int(match.group(1))
        episode_values = [int(item) for item in re.findall(r"\d+", match.group(2))]
        tokens.extend(f"S{season:02d}E{episode:02d}" for episode in episode_values)
    for match in _X_EPISODE.finditer(value):
        tokens.append(f"{int(match.group(1))}x{int(match.group(2)):02d}")
    for match in _WORD_EPISODE.finditer(value):
        tokens.append(f"Episode {int(match.group(1))}")
    return list(dict.fromkeys(tokens))


def _season_evidence(value: str) -> tuple[list[set[int]], list[str], bool]:
    sets: list[set[int]] = []
    patterns: list[str] = []
    explicit_range = False
    for regex in (_S_RANGE, _TEXT_RANGE):
        for match in regex.finditer(value):
            start, end = int(match.group(1)), int(match.group(2))
            if start <= end and end - start <= 200:
                sets.append(set(range(start, end + 1)))
                patterns.append(match.group(0))
                explicit_range = True

    range_spans = [
        match.span() for regex in (_S_RANGE, _TEXT_RANGE) for match in regex.finditer(value)
    ]
    s_values = {
        int(match.group(1))
        for match in _S_TOKEN.finditer(value)
        if not any(start <= match.start() and match.end() <= end for start, end in range_spans)
    }
    if s_values:
        sets.append(s_values)
        patterns.extend(f"S{season:02d}" for season in sorted(s_values))

    for match in _SEASON_LIST.finditer(value):
        values = {int(number) for number in re.findall(r"\d{1,3}", match.group(1))}
        if len(values) > 1:
            sets.append(values)
            patterns.append(match.group(0).strip())

    for match in _SEASON_NUMBER.finditer(value):
        number = int(match.group(1))
        if not any(number in seasons for seasons in sets):
            sets.append({number})
            patterns.append(match.group(0))
    ordinal = _ORDINAL_SEASON.search(value)
    if ordinal:
        number = _ORDINALS[ordinal.group(1).casefold()]
        sets.append({number})
        patterns.append(ordinal.group(0))
    return sets, list(dict.fromkeys(patterns)), explicit_range


def _is_single_season_pack(
    value: str,
    season_sets: list[set[int]],
    detected: set[int],
) -> bool:
    if len(detected) != 1:
        return False
    if any(len(item) > 1 for item in season_sets):
        return False
    return bool(
        _PACK.search(value)
        or _BARE_COMPLETE.search(value)
        or _SEASON_NUMBER.search(value)
        or _ORDINAL_SEASON.search(value)
        or _S_TOKEN.search(value)
    )
