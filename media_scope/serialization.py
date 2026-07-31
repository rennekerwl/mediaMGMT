"""Stable JSON serialization helpers."""

from __future__ import annotations

import json

from media_scope.models import JsonObject


def serialize_json(payload: JsonObject, *, pretty: bool = False) -> str:
    """Serialize a public response as UTF-8-compatible JSON with one trailing newline."""
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
