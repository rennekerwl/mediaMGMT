"""Stable JSON serialization helpers."""

from __future__ import annotations

import json
import sys

from media_scope.models import JsonObject


def configure_utf8_stdio() -> None:
    """Use UTF-8 for real process streams while leaving test capture streams intact."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def serialize_json(payload: JsonObject, *, pretty: bool = False) -> str:
    """Serialize a public response as UTF-8-compatible JSON with one trailing newline."""
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
