"""Validation tests for the Step 6-to-Step 7 handoff."""

from __future__ import annotations

from copy import deepcopy

import pytest

from media_scope.exceptions import TransferInputError
from media_scope.transfer_input import parse_transfer_input

HASH = "a" * 40


def transfer_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "result": "download_completed",
        "job_id": "download-1091-aaaaaaaaaaaa",
        "scope": {
            "media_type": "movie",
            "tmdb_id": 1091,
            "title": "The Thing",
            "year": 1982,
        },
        "candidate": {
            "infohash": HASH,
            "release_title": "The Thing 1982 1080p",
        },
        "storage": {
            "protocol": "sftp",
            "download_directory": "/downloads/1091-the-thing-aaaaaaaa",
        },
        "paths": {
            "final_base_path": "/downloads/1091-the-thing-aaaaaaaa/The Thing",
            "top_level_paths": ["/downloads/1091-the-thing-aaaaaaaa/The Thing"],
        },
        "status": "READY_FOR_TRANSFER",
        "ready_for_transfer": True,
    }


def test_valid_movie_handoff_is_parsed() -> None:
    parsed = parse_transfer_input(transfer_result())
    assert parsed.infohash == HASH
    assert parsed.scope["media_type"] == "movie"
    assert str(parsed.top_level_paths[0]).endswith("/The Thing")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("result", "download_failed"),
        ("status", "COMPLETED"),
        ("ready_for_transfer", False),
        ("job_id", ""),
    ],
)
def test_required_success_fields_are_enforced(field: str, value: object) -> None:
    payload = transfer_result()
    payload[field] = value
    with pytest.raises(TransferInputError):
        parse_transfer_input(payload)


def test_non_movie_handoff_is_rejected() -> None:
    payload = transfer_result()
    payload["scope"] = {"media_type": "tv", "tmdb_id": 4608, "title": "30 Rock"}
    with pytest.raises(TransferInputError) as captured:
        parse_transfer_input(payload)
    assert captured.value.error_code == "UNSUPPORTED_MEDIA_TYPE"


def test_invalid_hash_and_unsafe_paths_are_rejected() -> None:
    invalid_hash = transfer_result()
    invalid_hash["candidate"] = {"infohash": "bad"}
    with pytest.raises(TransferInputError):
        parse_transfer_input(invalid_hash)

    unsafe = transfer_result()
    unsafe["paths"] = {
        "final_base_path": "/downloads/movie",
        "top_level_paths": ["relative/movie.mkv"],
    }
    with pytest.raises(TransferInputError):
        parse_transfer_input(unsafe)


def test_duplicate_and_overlapping_sources_are_rejected() -> None:
    duplicate = transfer_result()
    duplicate["paths"] = {
        "final_base_path": "/downloads/job",
        "top_level_paths": ["/downloads/job/a", "/downloads/job/a"],
    }
    with pytest.raises(TransferInputError):
        parse_transfer_input(duplicate)

    overlapping = deepcopy(transfer_result())
    overlapping["paths"] = {
        "final_base_path": "/downloads/job",
        "top_level_paths": ["/downloads/job/a", "/downloads/job/a/b"],
    }
    with pytest.raises(TransferInputError):
        parse_transfer_input(overlapping)
