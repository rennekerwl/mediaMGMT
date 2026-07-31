"""Build eligible movie and complete-series acquisition scopes."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from media_scope.exceptions import InvalidResponseError
from media_scope.models import (
    EpisodeScope,
    Genre,
    JsonObject,
    ScopeWarning,
    SeasonScope,
)

if TYPE_CHECKING:
    from media_scope.client import TmdbClient


def build_movie_scope(details: JsonObject) -> JsonObject:
    """Build the public eligibility result for one movie."""
    tmdb_id = _required_int(details, "id", "movie")
    status = details.get("status")
    if not isinstance(status, str):
        raise InvalidResponseError("TMDb movie details did not contain a valid status.")

    title = _optional_string(details.get("title"))
    original_title = _optional_string(details.get("original_title"))
    release_date = _optional_string(details.get("release_date"))
    runtime = details.get("runtime") if isinstance(details.get("runtime"), int) else None
    warnings: list[ScopeWarning] = []
    if release_date is None:
        warnings.append(
            ScopeWarning("MOVIE_AIR_DATE_MISSING", "The movie is missing a release date.")
        )
    if runtime is None:
        warnings.append(ScopeWarning("MOVIE_RUNTIME_MISSING", "The movie is missing a runtime."))

    base: JsonObject = {
        "schema_version": 1,
        "result": "resolved",
        "eligible": status == "Released",
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "status": status,
    }
    if status != "Released":
        base.update(
            {
                "ineligibility_code": "MOVIE_NOT_RELEASED",
                "ineligibility_reason": (
                    "The MVP accepts only movies whose TMDb status is Released."
                ),
                "warnings": [warning.to_dict() for warning in warnings],
            }
        )
        return base

    base.update(
        {
            "scope_type": "single_movie",
            "release_date": release_date,
            "release_year": _year_from_date(release_date),
            "runtime_minutes": runtime,
            "original_language": _optional_string(details.get("original_language")),
            "genres": [genre.to_dict() for genre in _genres(details.get("genres"))],
            "scope": {"movie_count": 1},
            "warnings": [warning.to_dict() for warning in warnings],
        }
    )
    return base


def build_tv_scope(
    details: JsonObject,
    client: TmdbClient,
    *,
    today: date | None = None,
) -> JsonObject:
    """Build the public eligibility and complete regular-season scope for a TV series."""
    current_date = today or date.today()
    tmdb_id = _required_int(details, "id", "television series")
    status = details.get("status")
    if not isinstance(status, str):
        raise InvalidResponseError("TMDb television details did not contain a valid status.")

    title = _optional_string(details.get("name"))
    original_title = _optional_string(details.get("original_name"))
    base: JsonObject = {
        "schema_version": 1,
        "result": "resolved",
        "eligible": status == "Ended",
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "status": status,
    }
    if status != "Ended":
        base.update(
            {
                "ineligibility_code": "TV_SERIES_NOT_ENDED",
                "ineligibility_reason": (
                    "The MVP accepts only television series whose TMDb status is Ended."
                ),
                "warnings": [],
            }
        )
        return base

    warnings: list[ScopeWarning] = []
    fatal = False
    summaries = details.get("seasons")
    included_summaries: list[tuple[int, int]] = []
    seen_season_numbers: set[int] = set()
    if not isinstance(summaries, list):
        warnings.append(
            ScopeWarning(
                "SEASON_SUMMARIES_MISSING",
                "TMDb did not return a valid list of season summaries.",
            )
        )
        fatal = True
        summaries = []

    for summary in summaries:
        if not isinstance(summary, dict):
            warnings.append(
                ScopeWarning(
                    "SEASON_SUMMARY_INVALID",
                    "TMDb returned a season summary with an invalid structure.",
                )
            )
            fatal = True
            continue
        season_number = summary.get("season_number")
        if not isinstance(season_number, int):
            warnings.append(
                ScopeWarning(
                    "SEASON_NUMBER_MISSING",
                    "A TMDb season summary is missing its season number.",
                )
            )
            fatal = True
            continue
        if season_number <= 0:
            continue
        if season_number in seen_season_numbers:
            warnings.append(
                ScopeWarning(
                    "DUPLICATE_SEASON_NUMBER",
                    "TMDb returned duplicate regular-season summaries.",
                    season_number=season_number,
                )
            )
            fatal = True
            continue
        seen_season_numbers.add(season_number)
        episode_count = summary.get("episode_count")
        if not isinstance(episode_count, int) or episode_count < 0:
            warnings.append(
                ScopeWarning(
                    "SEASON_EPISODE_COUNT_INVALID",
                    "A regular-season summary has no valid episode count.",
                    season_number=season_number,
                )
            )
            fatal = True
            continue
        if episode_count > 0:
            included_summaries.append((season_number, episode_count))

    seasons: list[SeasonScope] = []
    episode_numbers_seen: set[tuple[int, int]] = set()
    for season_number, summary_count in sorted(included_summaries):
        season_details = client.get_tv_season(tmdb_id, season_number)
        season, season_warnings, season_fatal = _build_season(
            season_details=season_details,
            expected_season_number=season_number,
            summary_count=summary_count,
            episode_numbers_seen=episode_numbers_seen,
            today=current_date,
        )
        warnings.extend(season_warnings)
        fatal = fatal or season_fatal
        if season is not None:
            seasons.append(season)

    seasons.sort(key=lambda season: season.season_number)
    calculated_episode_count = sum(len(season.episodes) for season in seasons)
    if not included_summaries or calculated_episode_count == 0:
        warnings.append(
            ScopeWarning(
                "NO_REGULAR_EPISODES",
                "No complete regular-season episode scope could be determined.",
            )
        )
        fatal = True

    reported_total = details.get("number_of_episodes")
    if isinstance(reported_total, int) and reported_total != calculated_episode_count:
        warnings.append(
            ScopeWarning(
                "EPISODE_TOTAL_MISMATCH",
                (
                    f"TMDb reports {reported_total} total episodes, while the included "
                    f"regular-season details contain {calculated_episode_count}."
                ),
            )
        )

    if fatal:
        base.update(
            {
                "eligible": False,
                "ineligibility_code": "SCOPE_INCOMPLETE",
                "ineligibility_reason": (
                    "The complete regular-season episode scope could not be determined "
                    "reliably from TMDb metadata."
                ),
                "warnings": [warning.to_dict() for warning in warnings],
            }
        )
        return base

    first_air_date = _optional_string(details.get("first_air_date"))
    base.update(
        {
            "scope_type": "complete_series",
            "first_air_date": first_air_date,
            "last_air_date": _optional_string(details.get("last_air_date")),
            "first_air_year": _year_from_date(first_air_date),
            "original_language": _optional_string(details.get("original_language")),
            "genres": [genre.to_dict() for genre in _genres(details.get("genres"))],
            "scope_policy": {
                "require_status": "Ended",
                "include_regular_seasons": True,
                "include_season_zero": False,
                "include_specials": False,
                "episode_order": "TMDb standard season ordering",
            },
            "scope": {
                "season_count": len(seasons),
                "episode_count": calculated_episode_count,
                "seasons": [season.to_dict() for season in seasons],
            },
            "warnings": [warning.to_dict() for warning in warnings],
        }
    )
    return base


def _build_season(
    *,
    season_details: JsonObject,
    expected_season_number: int,
    summary_count: int,
    episode_numbers_seen: set[tuple[int, int]],
    today: date,
) -> tuple[SeasonScope | None, list[ScopeWarning], bool]:
    warnings: list[ScopeWarning] = []
    fatal = False
    season_id = season_details.get("id")
    detail_number = season_details.get("season_number")
    if not isinstance(season_id, int):
        warnings.append(
            ScopeWarning(
                "SEASON_ID_MISSING",
                "A regular-season detail response is missing its TMDb ID.",
                season_number=expected_season_number,
            )
        )
        fatal = True
    if not isinstance(detail_number, int) or detail_number != expected_season_number:
        warnings.append(
            ScopeWarning(
                "SEASON_NUMBER_INCONSISTENT",
                "A season-detail response has a missing or inconsistent season number.",
                season_number=expected_season_number,
            )
        )
        fatal = True

    air_date = _optional_string(season_details.get("air_date"))
    if air_date is None:
        warnings.append(
            ScopeWarning(
                "SEASON_AIR_DATE_MISSING",
                "A regular season is missing an air date.",
                season_number=expected_season_number,
                tmdb_id=season_id if isinstance(season_id, int) else None,
            )
        )

    episode_values = season_details.get("episodes")
    if not isinstance(episode_values, list):
        warnings.append(
            ScopeWarning(
                "SEASON_EPISODES_INVALID",
                "A regular-season detail response has no valid episode list.",
                season_number=expected_season_number,
            )
        )
        return None, warnings, True
    if not episode_values:
        warnings.append(
            ScopeWarning(
                "SEASON_DETAILS_EMPTY",
                "The season summary reports episodes, but season details contain none.",
                season_number=expected_season_number,
                tmdb_id=season_id if isinstance(season_id, int) else None,
            )
        )
        return None, warnings, True
    if len(episode_values) != summary_count:
        warnings.append(
            ScopeWarning(
                "SEASON_EPISODE_COUNT_MISMATCH",
                (
                    f"The season summary reports {summary_count} episodes, but season "
                    f"details contain {len(episode_values)}."
                ),
                season_number=expected_season_number,
            )
        )

    episodes: list[EpisodeScope] = []
    for value in episode_values:
        if not isinstance(value, dict):
            warnings.append(
                ScopeWarning(
                    "EPISODE_RECORD_INVALID",
                    "TMDb returned an episode with an invalid structure.",
                    season_number=expected_season_number,
                )
            )
            fatal = True
            continue
        episode_id = value.get("id")
        season_number = value.get("season_number")
        episode_number = value.get("episode_number")
        if (
            not isinstance(episode_id, int)
            or not isinstance(season_number, int)
            or not isinstance(episode_number, int)
            or season_number != expected_season_number
        ):
            warnings.append(
                ScopeWarning(
                    "EPISODE_IDENTITY_INCOMPLETE",
                    "An episode has a missing or inconsistent ID, season, or episode number.",
                    season_number=expected_season_number,
                    episode_number=episode_number if isinstance(episode_number, int) else None,
                    tmdb_id=episode_id if isinstance(episode_id, int) else None,
                )
            )
            fatal = True
            continue

        number_key = (season_number, episode_number)
        if number_key in episode_numbers_seen:
            warnings.append(
                ScopeWarning(
                    "DUPLICATE_EPISODE_NUMBER",
                    "Two TMDb episodes have the same season and episode number.",
                    season_number=season_number,
                    episode_number=episode_number,
                    tmdb_id=episode_id,
                )
            )
            fatal = True
        episode_numbers_seen.add(number_key)

        episode_air_date = _optional_string(value.get("air_date"))
        runtime = value.get("runtime") if isinstance(value.get("runtime"), int) else None
        if episode_air_date is None:
            warnings.append(
                ScopeWarning(
                    "EPISODE_AIR_DATE_MISSING",
                    "An episode is missing an air date.",
                    season_number=season_number,
                    episode_number=episode_number,
                    tmdb_id=episode_id,
                )
            )
        else:
            parsed_date = _parse_date(episode_air_date)
            if parsed_date is None:
                warnings.append(
                    ScopeWarning(
                        "EPISODE_AIR_DATE_INVALID",
                        "An episode has an invalid air date.",
                        season_number=season_number,
                        episode_number=episode_number,
                        tmdb_id=episode_id,
                    )
                )
            elif parsed_date > today:
                warnings.append(
                    ScopeWarning(
                        "FUTURE_EPISODE_AIR_DATE",
                        "An ended series contains an episode with a future air date.",
                        season_number=season_number,
                        episode_number=episode_number,
                        tmdb_id=episode_id,
                    )
                )
        if runtime is None:
            warnings.append(
                ScopeWarning(
                    "EPISODE_RUNTIME_MISSING",
                    "An episode is missing a runtime.",
                    season_number=season_number,
                    episode_number=episode_number,
                    tmdb_id=episode_id,
                )
            )
        episodes.append(
            EpisodeScope(
                tmdb_episode_id=episode_id,
                season_number=season_number,
                episode_number=episode_number,
                name=_optional_string(value.get("name")),
                air_date=episode_air_date,
                runtime_minutes=runtime,
            )
        )

    episodes.sort(key=lambda episode: (episode.season_number, episode.episode_number))
    if not episodes:
        fatal = True
    if not isinstance(season_id, int):
        return None, warnings, True
    return (
        SeasonScope(
            tmdb_season_id=season_id,
            season_number=expected_season_number,
            name=_optional_string(season_details.get("name")),
            air_date=air_date,
            episodes=tuple(episodes),
        ),
        warnings,
        fatal,
    )


def _required_int(details: JsonObject, key: str, record_name: str) -> int:
    value = details.get(key)
    if not isinstance(value, int):
        raise InvalidResponseError(f"TMDb {record_name} details did not contain a valid {key}.")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _genres(value: object) -> tuple[Genre, ...]:
    if not isinstance(value, list):
        return ()
    genres: list[Genre] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        genre_id = item.get("id")
        name = item.get("name")
        if isinstance(genre_id, int) and isinstance(name, str):
            genres.append(Genre(genre_id, name))
    return tuple(genres)


def _year_from_date(value: str | None) -> int | None:
    if value is None or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
