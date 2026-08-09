"""Validation of the Step 6 result consumed by the movie transfer command."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from media_scope.exceptions import TransferInputError
from media_scope.jackett_client import normalize_infohash
from media_scope.models import JsonObject


@dataclass(frozen=True, slots=True)
class MovieTransferInput:
    """Validated movie download handoff ready for local transfer."""

    job_id: str
    scope: JsonObject
    candidate: JsonObject
    infohash: str
    download_directory: PurePosixPath
    final_base_path: PurePosixPath
    top_level_paths: tuple[PurePosixPath, ...]


def load_transfer_input(path: Path) -> MovieTransferInput:
    """Load one UTF-8 Step 6 result file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TransferInputError("Download-result input is not readable UTF-8 JSON.") from exc
    return load_transfer_input_text(raw)


def load_transfer_input_text(raw: str) -> MovieTransferInput:
    """Load one Step 6 result from piped JSON text."""
    if not raw.strip():
        raise TransferInputError("Download-result input was empty.")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TransferInputError("Download-result input is not valid JSON.") from exc
    return parse_transfer_input(value)


def parse_transfer_input(value: Any) -> MovieTransferInput:
    """Validate the Step 6 completion result and its transfer paths."""
    if not isinstance(value, dict):
        raise TransferInputError("Download-result input must be a JSON object.")
    if value.get("schema_version") != 1:
        raise TransferInputError("Download-result schema_version must be 1.")
    if value.get("result") != "download_completed":
        raise TransferInputError("Step 6 did not report download_completed.")
    if value.get("status") != "READY_FOR_TRANSFER" or value.get("ready_for_transfer") is not True:
        raise TransferInputError("Step 6 did not report READY_FOR_TRANSFER.")

    job_id = value.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise TransferInputError("Download-result input is missing a valid job_id.")

    scope = value.get("scope")
    if not isinstance(scope, dict):
        raise TransferInputError("Download-result input must contain a scope object.")
    if scope.get("media_type") != "movie":
        error = TransferInputError("The movie transfer command accepts only movie downloads.")
        error.error_code = "UNSUPPORTED_MEDIA_TYPE"
        raise error

    candidate = value.get("candidate")
    if not isinstance(candidate, dict):
        raise TransferInputError("Download-result input must contain a candidate object.")
    supplied_hash = candidate.get("infohash")
    if not isinstance(supplied_hash, str):
        raise TransferInputError("The completed candidate is missing infohash.")
    infohash = normalize_infohash(supplied_hash)
    if infohash is None:
        raise TransferInputError("The completed candidate has an invalid infohash.")

    storage = value.get("storage")
    if not isinstance(storage, dict) or storage.get("protocol") != "sftp":
        raise TransferInputError("Download-result storage must use SFTP.")
    download_directory = _absolute_remote_path(
        storage.get("download_directory"), "storage.download_directory"
    )

    paths = value.get("paths")
    if not isinstance(paths, dict):
        raise TransferInputError("Download-result input must contain a paths object.")
    final_base_path = _absolute_remote_path(paths.get("final_base_path"), "paths.final_base_path")
    raw_top_level = paths.get("top_level_paths")
    if not isinstance(raw_top_level, list) or not raw_top_level:
        raise TransferInputError("paths.top_level_paths must be a nonempty list.")
    top_level = tuple(
        _absolute_remote_path(item, "paths.top_level_paths entry") for item in raw_top_level
    )
    if len(set(top_level)) != len(top_level):
        raise TransferInputError("paths.top_level_paths contains duplicate paths.")
    for index, path in enumerate(top_level):
        for other in top_level[index + 1 :]:
            if path in other.parents or other in path.parents:
                raise TransferInputError("paths.top_level_paths contains overlapping paths.")

    return MovieTransferInput(
        job_id=job_id.strip(),
        scope=dict(scope),
        candidate=dict(candidate),
        infohash=infohash,
        download_directory=download_directory,
        final_base_path=final_base_path,
        top_level_paths=top_level,
    )


def _absolute_remote_path(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise TransferInputError(f"{field} must be a nonempty absolute POSIX path.")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise TransferInputError(f"{field} must be a safe absolute POSIX path.")
    return path
