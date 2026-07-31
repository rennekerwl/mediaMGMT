"""Tests for the validated Jackett-to-probe handoff."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from media_scope.exceptions import NoProbeCandidatesError, ProbeInputError
from media_scope.probe_input import load_probe_input, parse_probe_input

HASH_A = "a" * 40
HASH_B = "b" * 40


def candidate(rank: int, infohash: str = HASH_A) -> dict[str, Any]:
    return {
        "rank": rank,
        "classification": "COMPLETE_SERIES",
        "original_title": f"Release {rank}",
        "magnet_uri": f"magnet:?xt=urn:btih:{infohash}",
        "infohash": infohash,
        "size_bytes": 123,
        "seeders": 4,
        "score": 90 - rank,
        "source_indexers": ["test"],
    }


def report(*candidates: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": "search_completed",
        "scope": {"tmdb_id": 1, "title": "Example", "expected_seasons": [1]},
        "candidates": list(candidates),
    }


def test_valid_search_json_loads_and_sorts_by_original_rank(tmp_path: Path) -> None:
    path = tmp_path / "search.json"
    path.write_text(json.dumps(report(candidate(2, HASH_B), candidate(1))), encoding="utf-8")
    value = load_probe_input(path)
    assert [item.rank for item in value.candidates] == [1, 2]
    assert value.candidates[0].infohash == HASH_A


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"result": "error", "scope": {}, "candidates": []},
        {"result": "search_completed", "candidates": []},
        {"result": "search_completed", "scope": {}, "candidates": "bad"},
    ],
)
def test_invalid_search_shapes_are_rejected(value: Any) -> None:
    with pytest.raises(ProbeInputError):
        parse_probe_input(value)


def test_missing_candidates_has_distinct_valid_input_outcome() -> None:
    with pytest.raises(NoProbeCandidatesError) as captured:
        parse_probe_input(report())
    assert captured.value.error_code == "NO_CANDIDATES"


@pytest.mark.parametrize(
    ("change", "error_code"),
    [
        ({"magnet_uri": None}, "INVALID_SEARCH_RESULTS"),
        ({"magnet_uri": "https://example.invalid/file"}, "INVALID_MAGNET"),
        ({"magnet_uri": "magnet:?xt=urn:btih:short"}, "INVALID_MAGNET"),
        ({"magnet_uri": f"magnet:?xt=urn:btih:{HASH_A}\n"}, "INVALID_MAGNET"),
        ({"infohash": HASH_B}, "INFOHASH_MISMATCH"),
        ({"rank": 0}, "INVALID_SEARCH_RESULTS"),
    ],
)
def test_malformed_candidate_is_rejected(change: dict[str, Any], error_code: str) -> None:
    value = candidate(1)
    value.update(change)
    with pytest.raises(ProbeInputError) as captured:
        parse_probe_input(report(value))
    assert captured.value.error_code == error_code


def test_duplicate_ranks_are_rejected() -> None:
    with pytest.raises(ProbeInputError):
        parse_probe_input(report(candidate(1), candidate(1, HASH_B)))


def test_invalid_json_file_is_structured_input_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ProbeInputError):
        load_probe_input(path)


def test_non_utf8_file_is_structured_input_error(tmp_path: Path) -> None:
    path = tmp_path / "binary.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ProbeInputError):
        load_probe_input(path)
