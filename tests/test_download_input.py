"""Tests for the Step 5-to-Step 6 JSON handoff."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from media_scope.download_input import load_download_input, parse_download_input
from media_scope.exceptions import DownloadInputError

HASH_A = "a" * 40
HASH_B = "b" * 40


def health_result(**selected_changes: Any) -> dict[str, Any]:
    selected = {
        "original_rank": 2,
        "infohash": HASH_A,
        "magnet_uri": f"magnet:?xt=urn:btih:{HASH_A}",
        "release_title": "Example Complete",
        "rtorrent_hash": HASH_A.upper(),
        "status": "READY_FOR_DOWNLOAD",
    }
    selected.update(selected_changes)
    return {
        "schema_version": 1,
        "result": "candidate_health_validated",
        "job_id": "probe-example",
        "scope": {"tmdb_id": 4608, "title": "30 Rock", "expected_seasons": [1, 2]},
        "selected_candidate": selected,
    }


def test_valid_health_result_normalizes_hashes(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    path.write_text(json.dumps(health_result()), encoding="utf-8")
    parsed = load_download_input(path)
    assert parsed.candidate.infohash == HASH_A
    assert parsed.candidate.rtorrent_hash == HASH_A
    assert parsed.probe_job_id == "probe-example"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ({}, "INVALID_HEALTH_RESULT"),
        (
            {"result": "candidate_health_validated", "scope": {}, "selected_candidate": None},
            "NO_SELECTED_CANDIDATE",
        ),
        (health_result(infohash="short"), "INVALID_HEALTH_RESULT"),
        (health_result(rtorrent_hash=HASH_B), "INFOHASH_MISMATCH"),
        (
            health_result(magnet_uri=f"magnet:?xt=urn:btih:{HASH_B}"),
            "INFOHASH_MISMATCH",
        ),
        (health_result(status="METADATA_RETRIEVED"), "INVALID_HEALTH_RESULT"),
    ],
)
def test_invalid_handoffs_are_rejected(value: Any, code: str) -> None:
    with pytest.raises(DownloadInputError) as captured:
        parse_download_input(value)
    assert captured.value.error_code == code


def test_invalid_json_is_structured_input_error(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(DownloadInputError):
        load_download_input(path)
