"""Validation of the Jackett-search JSON handoff consumed by Step 5."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from media_scope.exceptions import NoProbeCandidatesError, ProbeInputError
from media_scope.jackett_client import normalize_infohash
from media_scope.magnet_resolver import MagnetResolutionError, validate_magnet_uri
from media_scope.probe_models import ProbeCandidate, SearchProbeInput, copy_json_object


def load_probe_input(path: Path) -> SearchProbeInput:
    """Load and completely validate one search-results file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeInputError("Search-results input is not valid UTF-8 JSON.") from exc
    except OSError as exc:
        raise ProbeInputError(f"Could not read search-results JSON: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeInputError("Search-results input is not valid UTF-8 JSON.") from exc
    return parse_probe_input(value)


def parse_probe_input(value: Any) -> SearchProbeInput:
    """Validate a decoded Jackett-search report and sort candidates by rank."""
    if not isinstance(value, dict):
        raise ProbeInputError("Search-results JSON must be an object.")
    if value.get("result") != "search_completed":
        raise ProbeInputError("Search-results input did not complete successfully.")
    scope = value.get("scope")
    if not isinstance(scope, dict):
        raise ProbeInputError("Search-results input must contain a scope object.")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise ProbeInputError("Search-results input must contain a candidates array.")
    if not candidates:
        raise NoProbeCandidatesError("Search-results input contains no probeable candidates.")

    parsed: list[ProbeCandidate] = []
    ranks: set[int] = set()
    for position, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise ProbeInputError(f"Candidate {position} must be an object.")
        rank = candidate.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
            raise ProbeInputError(f"Candidate {position} has an invalid positive rank.")
        if rank in ranks:
            raise ProbeInputError(f"Candidate rank {rank} is duplicated.")
        ranks.add(rank)
        magnet = candidate.get("magnet_uri")
        if not isinstance(magnet, str):
            raise ProbeInputError(f"Candidate rank {rank} is missing magnet_uri.")
        try:
            validated = validate_magnet_uri(magnet)
        except MagnetResolutionError as exc:
            error = ProbeInputError(f"Candidate rank {rank} has an invalid magnet URI.")
            error.error_code = "INVALID_MAGNET"
            raise error from exc
        supplied_hash = candidate.get("infohash")
        if supplied_hash is not None and not isinstance(supplied_hash, str):
            raise ProbeInputError(f"Candidate rank {rank} has an invalid infohash.")
        normalized = normalize_infohash(supplied_hash) if supplied_hash is not None else None
        if supplied_hash is not None and normalized is None:
            raise ProbeInputError(f"Candidate rank {rank} has an invalid infohash.")
        if normalized is not None and normalized != validated.infohash:
            error = ProbeInputError(
                f"Candidate rank {rank} infohash does not match the magnet BTIH."
            )
            error.error_code = "INFOHASH_MISMATCH"
            raise error
        parsed.append(
            ProbeCandidate(
                rank=rank,
                magnet_uri=validated.magnet_uri,
                infohash=validated.infohash,
                raw=copy_json_object(candidate),
            )
        )
    parsed.sort(key=lambda item: item.rank)
    return SearchProbeInput(copy_json_object(scope), tuple(parsed))
