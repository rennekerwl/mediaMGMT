"""Television eligibility, ordering, warning, and failure tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

import pytest

from media_scope.scope_builder import build_tv_scope


class SeasonClient:
    """Return saved season details and record which seasons were requested."""

    def __init__(self, seasons: dict[int, dict[str, Any]]) -> None:
        self.seasons = seasons
        self.requested: list[int] = []

    def get_tv_season(self, _series_id: int, season_number: int) -> dict[str, Any]:
        self.requested.append(season_number)
        return deepcopy(self.seasons[season_number])


@pytest.fixture
def ended_details(load_fixture: Any) -> dict[str, Any]:
    return load_fixture("tv_ended.json")


@pytest.fixture
def season_client(load_fixture: Any) -> SeasonClient:
    return SeasonClient(
        {
            1: load_fixture("tv_season_1.json"),
            2: load_fixture("tv_season_2.json"),
        }
    )


@pytest.fixture
def returning_details(load_fixture: Any) -> dict[str, Any]:
    return load_fixture("tv_returning.json")


@pytest.fixture
def returning_client(load_fixture: Any) -> SeasonClient:
    return SeasonClient(
        {
            1: load_fixture("tv_season_1.json"),
            2: load_fixture("tv_season_2.json"),
            3: load_fixture("tv_season_3_future.json"),
        }
    )


def test_ended_series_is_eligible_and_sorted(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["eligible"] is True
    assert season_client.requested == [1, 2]
    assert result["scope"]["season_count"] == 2
    assert result["scope"]["episode_count"] == 3
    assert [season["season_number"] for season in result["scope"]["seasons"]] == [1, 2]
    assert [episode["episode_number"] for episode in result["scope"]["seasons"][0]["episodes"]] == [
        1,
        2,
    ]


@pytest.mark.parametrize("status", ["Returning Series", "Canceled"])
def test_non_ended_series_is_ineligible_without_season_requests(
    ended_details: dict[str, Any], season_client: SeasonClient, status: str
) -> None:
    ended_details["status"] = status

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["ineligibility_code"] == "TV_SERIES_NOT_ENDED"
    assert season_client.requested == []


def test_season_zero_is_excluded(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert 0 not in season_client.requested


def test_empty_season_details_warn_and_make_scope_incomplete(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    season_client.seasons[1]["episodes"] = []

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["eligible"] is False
    assert result["ineligibility_code"] == "SCOPE_INCOMPLETE"
    assert "scope" not in result
    assert "SEASON_DETAILS_EMPTY" in warning_codes(result)


def test_duplicate_episode_number_makes_scope_incomplete(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    season_client.seasons[1]["episodes"][1]["episode_number"] = 2

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["ineligibility_code"] == "SCOPE_INCOMPLETE"
    assert "DUPLICATE_EPISODE_NUMBER" in warning_codes(result)


def test_future_episode_air_date_is_nonfatal_warning(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    season_client.seasons[2]["episodes"][0]["air_date"] = "2030-01-01"

    result = build_tv_scope(
        ended_details,
        season_client,  # type: ignore[arg-type]
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert "FUTURE_EPISODE_AIR_DATE" in warning_codes(result)


def test_missing_episode_metadata_produces_warnings(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    season_client.seasons[1]["air_date"] = None
    season_client.seasons[1]["episodes"][0]["air_date"] = None
    season_client.seasons[1]["episodes"][0]["runtime"] = None

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["eligible"] is True
    assert {
        "SEASON_AIR_DATE_MISSING",
        "EPISODE_AIR_DATE_MISSING",
        "EPISODE_RUNTIME_MISSING",
    } <= warning_codes(result)


def test_reported_episode_total_mismatch_warns(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    ended_details["number_of_episodes"] = 100

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["eligible"] is True
    assert "EPISODE_TOTAL_MISMATCH" in warning_codes(result)


def test_missing_episode_identity_makes_scope_incomplete(
    ended_details: dict[str, Any], season_client: SeasonClient
) -> None:
    del season_client.seasons[1]["episodes"][0]["id"]

    result = build_tv_scope(ended_details, season_client)  # type: ignore[arg-type]

    assert result["ineligibility_code"] == "SCOPE_INCOMPLETE"
    assert "EPISODE_IDENTITY_INCOMPLETE" in warning_codes(result)


def test_returning_series_remains_ineligible_without_opt_in(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    result = build_tv_scope(returning_details, returning_client)  # type: ignore[arg-type]

    assert result["ineligibility_code"] == "TV_SERIES_NOT_ENDED"
    assert returning_client.requested == []


def test_latest_complete_season_selects_only_prior_completed_season(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert result["scope_type"] == "latest_complete_season"
    assert result["scope"]["season_count"] == 1
    assert result["scope"]["episode_count"] == 1
    assert [season["season_number"] for season in result["scope"]["seasons"]] == [2]
    assert returning_client.requested == [2]
    assert result["scope_policy"]["require_later_season_evidence"] is True
    assert "EPISODE_TOTAL_MISMATCH" not in warning_codes(result)


def test_future_candidate_is_skipped_before_completed_prior_season(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_details["next_episode_to_air"] = None

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert result["scope"]["seasons"][0]["season_number"] == 2
    assert returning_client.requested == [3, 2]


def test_next_episode_in_later_season_proves_candidate_complete(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_details["seasons"] = [
        summary for summary in returning_details["seasons"] if summary["season_number"] <= 2
    ]
    returning_details["next_episode_to_air"]["season_number"] = 3

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert result["scope"]["seasons"][0]["season_number"] == 2
    assert returning_client.requested == [2]


def test_unverified_highest_season_falls_back_conservatively(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_details["seasons"] = [
        summary for summary in returning_details["seasons"] if summary["season_number"] <= 2
    ]
    returning_details["next_episode_to_air"] = None

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert result["scope"]["seasons"][0]["season_number"] == 1
    assert returning_client.requested == [1]


def test_no_provably_completed_season_is_ineligible(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_details["seasons"] = [
        summary for summary in returning_details["seasons"] if summary["season_number"] in (0, 1)
    ]
    returning_details["next_episode_to_air"] = None

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
    )

    assert result["eligible"] is False
    assert result["ineligibility_code"] == "NO_COMPLETED_SEASON"
    assert returning_client.requested == []


@pytest.mark.parametrize(
    "metadata_change",
    ["missing_air_date", "invalid_air_date", "count_mismatch", "duplicate_episode"],
)
def test_inconsistent_completed_candidate_makes_scope_incomplete(
    returning_details: dict[str, Any],
    returning_client: SeasonClient,
    metadata_change: str,
) -> None:
    returning_details["seasons"] = [
        summary for summary in returning_details["seasons"] if summary["season_number"] in (0, 1, 2)
    ]
    returning_details["seasons"][-1]["episode_count"] = 0
    returning_details["next_episode_to_air"] = None
    if metadata_change == "missing_air_date":
        returning_client.seasons[1]["episodes"][0]["air_date"] = None
    elif metadata_change == "invalid_air_date":
        returning_client.seasons[1]["episodes"][0]["air_date"] = "not-a-date"
    elif metadata_change == "count_mismatch":
        returning_details["seasons"][1]["episode_count"] = 3
    else:
        returning_client.seasons[1]["episodes"][1]["episode_number"] = 2

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is False
    assert result["ineligibility_code"] == "SCOPE_INCOMPLETE"


def test_selected_season_preserves_nonfatal_metadata_warnings(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_client.seasons[2]["air_date"] = None
    returning_client.seasons[2]["episodes"][0]["runtime"] = None

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
        today=date(2026, 1, 1),
    )

    assert result["eligible"] is True
    assert {
        "SEASON_AIR_DATE_MISSING",
        "EPISODE_RUNTIME_MISSING",
    } <= warning_codes(result)


def test_malformed_next_episode_metadata_makes_scope_incomplete(
    returning_details: dict[str, Any], returning_client: SeasonClient
) -> None:
    returning_details["next_episode_to_air"] = {"episode_number": 1}

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
    )

    assert result["ineligibility_code"] == "SCOPE_INCOMPLETE"
    assert "NEXT_EPISODE_METADATA_INVALID" in warning_codes(result)
    assert returning_client.requested == []


@pytest.mark.parametrize("status", ["Ended", "Canceled", "In Production"])
def test_latest_complete_mode_rejects_non_returning_statuses(
    returning_details: dict[str, Any], returning_client: SeasonClient, status: str
) -> None:
    returning_details["status"] = status

    result = build_tv_scope(
        returning_details,
        returning_client,  # type: ignore[arg-type]
        mode="latest_complete_season",
    )

    assert result["ineligibility_code"] == "TV_SERIES_NOT_RETURNING"
    assert returning_client.requested == []


def warning_codes(result: dict[str, Any]) -> set[str]:
    return {warning["code"] for warning in result["warnings"]}
