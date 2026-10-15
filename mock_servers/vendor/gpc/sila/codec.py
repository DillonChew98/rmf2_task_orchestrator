"""JSON helpers for SiLA String responses carrying GPC payloads."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any


def dumps_json(data: Any) -> str:
    """Serialize a value to a compact UTF-8 JSON string for SiLA String responses."""
    if data is None:
        return "null"
    if is_dataclass(data) and not isinstance(data, type):
        data = asdict(data)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def loads_mapping(payload_json: str) -> dict[str, Any]:
    """Parse a JSON object payload, raising ValueError on non-object inputs."""
    value = json.loads(payload_json or "{}")
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object.")
    return value


def loads_list(payload_json: str) -> list[Any]:
    """Parse a JSON array payload, raising ValueError on non-array inputs."""
    value = json.loads(payload_json or "[]")
    if not isinstance(value, list):
        raise ValueError("JSON payload must be an array.")
    return value


def timeout_opt(seconds: int) -> int | None:
    """Map the SiLA timeout sentinel: 0 means 'use the underlying default'."""
    return None if seconds <= 0 else seconds
