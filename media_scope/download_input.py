"""Validation of the Step 5 health-result handoff consumed by Step 6."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from media_scope.download_models import DownloadCandidate, HealthDownloadInput
from media_scope.exceptions import DownloadInputError
from media_scope.jackett_client import normalize_infohash
from media_scope.magnet_resolver import MagnetResolutionError, validate_magnet_uri


def load_download_input(path: Path) -> HealthDownloadInput:
    """Load one UTF-8 Step 5 result file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DownloadInputError("Health-result input is not readable UTF-8 JSON.") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DownloadInputError("Health-result input is not valid JSON.") from exc
    return parse_download_input(value)


def parse_download_input(value: Any) -> HealthDownloadInput:
    """Validate Step 5 success, selected candidate, and all hash identities."""
    if not isinstance(value, dict) or value.get("result") != "candidate_health_validated":
        raise DownloadInputError("Step 5 did not report candidate_health_validated.")
    scope = value.get("scope")
    if not isinstance(scope, dict):
        raise DownloadInputError("Health-result input must contain a scope object.")
    selected = value.get("selected_candidate")
    if not isinstance(selected, dict):
        error = DownloadInputError("Step 5 did not select a candidate.")
        error.error_code = "NO_SELECTED_CANDIDATE"
        raise error
    if selected.get("status") != "READY_FOR_DOWNLOAD":
        raise DownloadInputError("The selected candidate is not READY_FOR_DOWNLOAD.")

    supplied_hash = selected.get("infohash")
    rpc_hash = selected.get("rtorrent_hash")
    if not isinstance(supplied_hash, str) or normalize_infohash(supplied_hash) is None:
        raise DownloadInputError("The selected candidate has an invalid infohash.")
    infohash = normalize_infohash(supplied_hash)
    if not isinstance(rpc_hash, str) or normalize_infohash(rpc_hash) is None:
        error = DownloadInputError("The selected candidate has an invalid rtorrent_hash.")
        error.error_code = "INFOHASH_MISMATCH"
        raise error
    if normalize_infohash(rpc_hash) != infohash:
        error = DownloadInputError("infohash and rtorrent_hash identify different torrents.")
        error.error_code = "INFOHASH_MISMATCH"
        raise error

    magnet = selected.get("magnet_uri")
    if not isinstance(magnet, str):
        raise DownloadInputError("The selected candidate is missing magnet_uri.")
    try:
        validated = validate_magnet_uri(magnet)
    except MagnetResolutionError as exc:
        raise DownloadInputError("The selected candidate has an invalid magnet URI.") from exc
    if validated.infohash != infohash:
        error = DownloadInputError("The selected candidate magnet has a different BTIH hash.")
        error.error_code = "INFOHASH_MISMATCH"
        raise error

    title = selected.get("release_title")
    if not isinstance(title, str) or not title.strip():
        raise DownloadInputError("The selected candidate is missing release_title.")
    rank = selected.get("original_rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0):
        raise DownloadInputError("The selected candidate has an invalid original_rank.")
    probe_job_id = value.get("job_id")
    if probe_job_id is not None and not isinstance(probe_job_id, str):
        raise DownloadInputError("The Step 5 job_id must be a string when present.")
    candidate = DownloadCandidate(
        original_rank=rank,
        infohash=infohash,
        magnet_uri=validated.magnet_uri,
        release_title=title.strip(),
        rtorrent_hash=normalize_infohash(rpc_hash) or "",
        raw=dict(selected),
    )
    return HealthDownloadInput(dict(scope), candidate, probe_job_id)
