"""Release-title classification and title-matching tests."""

from __future__ import annotations

from media_scope.release_classifier import classify_release, normalize_release_title
from media_scope.search_models import (
    RawRelease,
    ReleaseClassification,
    SearchScope,
    TorznabCategory,
)

SCOPE = SearchScope(1, "Example Show", "Example Show", 2017, "Ended", (1, 2, 3, 4, 5))


def release(
    title: str,
    *,
    categories: tuple[TorznabCategory, ...] = (TorznabCategory(5000, "TV"),),
    guid: str | None = "guid",
) -> RawRelease:
    return RawRelease(
        sequence=0,
        indexer_id="alpha",
        indexer_name="Alpha",
        query="query",
        original_title=title,
        normalized_title=normalize_release_title(title),
        categories=categories,
        guid=guid,
    )


def test_episode_patterns_are_rejected() -> None:
    single = classify_release(release("Example.Show.S02E04.1080p"), SCOPE)
    chained = classify_release(release("Example.Show.S02E04E05.WEB-DL"), SCOPE)
    x_style = classify_release(release("Example Show 2x04 HDTV"), SCOPE)
    word = classify_release(release("Example Show Episode 7"), SCOPE)
    assert single.classification == ReleaseClassification.SINGLE_EPISODE
    assert chained.classification == ReleaseClassification.MULTI_EPISODE_PARTIAL
    assert x_style.classification == ReleaseClassification.SINGLE_EPISODE
    assert word.classification == ReleaseClassification.SINGLE_EPISODE
    assert not any(value.accepted_as_candidate for value in (single, chained, x_style, word))


def test_single_season_pack_and_ordinal_are_detected() -> None:
    numeric = classify_release(release("Example Show Season 2 Complete"), SCOPE)
    ordinal = classify_release(release("Example Show Complete Second Season"), SCOPE)
    assert numeric.classification == ReleaseClassification.SINGLE_SEASON_PACK
    assert ordinal.classification == ReleaseClassification.SINGLE_SEASON_PACK
    assert numeric.detected_seasons == (2,)
    assert not numeric.accepted_as_candidate


def test_one_season_scope_accepts_matching_single_season_pack() -> None:
    scope = SearchScope(1, "Example Show", None, 2017, "Ended", (1,))
    value = classify_release(release("Example.Show.S01.Complete"), scope)
    assert value.classification == ReleaseClassification.SINGLE_SEASON_PACK
    assert value.accepted_as_candidate


def test_multi_season_coverage_requires_all_expected_seasons() -> None:
    partial = classify_release(release("Example.Show.S01-S03"), SCOPE)
    missing_last = classify_release(release("Example.Show.S01-S04"), SCOPE)
    missing_first = classify_release(release("Example Show Seasons 2-5"), SCOPE)
    complete = classify_release(release("Example.Show.S01-S05"), SCOPE)
    assert partial.classification == ReleaseClassification.MULTI_SEASON_PACK
    assert missing_last.missing_seasons == (5,)
    assert missing_first.missing_seasons == (1,)
    assert complete.classification == ReleaseClassification.COMPLETE_SERIES
    assert complete.accepted_as_candidate


def test_explicit_season_list_can_cover_the_full_scope() -> None:
    scope = SearchScope(1, "Example Show", None, None, "Ended", (1, 2, 3))
    value = classify_release(release("Example.Show.S01.S02.S03.1080p"), scope)
    assert value.detected_seasons == (1, 2, 3)
    assert value.accepted_as_candidate


def test_strong_complete_phrase_accepts_but_bare_complete_does_not() -> None:
    strong = classify_release(release("Example Show The Complete Series 1080p"), SCOPE)
    collection = classify_release(release("Example Show Complete Collection BluRay"), SCOPE)
    bare = classify_release(release("Example Show Complete 1080p"), SCOPE)
    assert strong.classification == ReleaseClassification.COMPLETE_SERIES
    assert collection.accepted_as_candidate
    assert bare.classification == ReleaseClassification.COMPLETE_SERIES_CANDIDATE
    assert not bare.accepted_as_candidate


def test_all_seasons_count_must_match_expected_scope() -> None:
    matching = classify_release(release("Example Show All 5 Seasons"), SCOPE)
    conflicting = classify_release(release("Example Show All 4 Seasons"), SCOPE)
    assert matching.accepted_as_candidate
    assert conflicting.classification == ReleaseClassification.COMPLETE_SERIES_CANDIDATE
    assert not conflicting.accepted_as_candidate
    assert conflicting.conflicting_seasons


def test_conflicting_season_tokens_warn_and_are_not_accepted() -> None:
    value = classify_release(
        release("Example Show Complete Series S01-S05 S01-S04"),
        SCOPE,
    )
    assert value.classification == ReleaseClassification.COMPLETE_SERIES_CANDIDATE
    assert value.conflicting_seasons
    assert not value.accepted_as_candidate
    assert value.warnings


def test_extra_season_is_candidate_with_warning() -> None:
    value = classify_release(release("Example.Show.S01-S06.Complete"), SCOPE)
    assert value.classification == ReleaseClassification.COMPLETE_SERIES_CANDIDATE
    assert value.accepted_as_candidate
    assert value.extra_seasons == (6,)


def test_anime_category_and_known_anime_group_are_rejected() -> None:
    category = classify_release(
        release(
            "Example Show Complete Series",
            categories=(TorznabCategory(5070, "TV/Anime"),),
        ),
        SCOPE,
    )
    group = classify_release(release("[SubsPlease] Example Show Complete Series"), SCOPE)
    assert category.classification == ReleaseClassification.ANIME
    assert group.classification == ReleaseClassification.ANIME


def test_unrelated_and_unsafe_prefix_titles_are_rejected() -> None:
    unrelated = classify_release(release("Different Show Complete Series"), SCOPE)
    prefix = classify_release(release("Example Show Returns Complete Series"), SCOPE)
    assert unrelated.classification == ReleaseClassification.MOVIE_OR_UNRELATED
    assert prefix.classification == ReleaseClassification.MOVIE_OR_UNRELATED


def test_codec_resolution_and_release_tokens_do_not_break_title_match() -> None:
    value = classify_release(
        release("Example.Show.S01-S05.2160p.WEB-DL.HEVC.REPACK"),
        SCOPE,
    )
    assert value.title_match
    assert value.accepted_as_candidate


def test_year_supports_matching_but_is_not_required() -> None:
    with_year = classify_release(
        release("Example Show 2017 Complete Series 1080p"),
        SCOPE,
    )
    without_year = classify_release(release("Example Show Complete Series 1080p"), SCOPE)
    wrong_year = classify_release(release("Example Show 2018 Complete Series"), SCOPE)
    assert with_year.year_match
    assert without_year.accepted_as_candidate
    assert wrong_year.accepted_as_candidate
    assert wrong_year.warnings


def test_contradictory_non_tv_category_rejects() -> None:
    value = classify_release(
        release(
            "Example Show Complete Series",
            categories=(TorznabCategory(2000, "Movies"),),
        ),
        SCOPE,
    )
    assert value.classification == ReleaseClassification.MOVIE_OR_UNRELATED
    assert not value.accepted_as_candidate


def test_no_usable_reference_prevents_acceptance() -> None:
    value = classify_release(release("Example Show Complete Series", guid=None), SCOPE)
    assert not value.accepted_as_candidate
