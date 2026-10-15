"""JSON helpers for SiLA String payloads."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


class _NumpyEncoder(json.JSONEncoder):
    """Serialize numpy arrays and scalars transparently when numpy is installed."""

    def default(self, o: Any) -> Any:
        if _HAS_NUMPY and isinstance(o, np.ndarray):
            return o.tolist()
        if _HAS_NUMPY and isinstance(o, np.generic):
            return o.item()
        return super().default(o)


def dumps_json(data: Any, *, numpy: bool | None = None) -> str:
    """Serialize a value to a compact UTF-8 JSON string for SiLA String responses.

    When *numpy* is True (or None and numpy is importable), numpy arrays/scalars
    and dataclasses are handled. Pass ``numpy=False`` for plain JSON (TRIOS).
    """
    if data is None:
        return "null"
    use_numpy = _HAS_NUMPY if numpy is None else bool(numpy and _HAS_NUMPY)
    if use_numpy and is_dataclass(data) and not isinstance(data, type):
        data = asdict(data)
    if use_numpy:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), cls=_NumpyEncoder)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def loads_json(payload_json: str | None) -> Any:
    """Parse a JSON string; empty / whitespace / ``null`` means ``None``."""
    if payload_json is None:
        return None
    text = payload_json.strip()
    if not text or text == "null":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc


def loads_mapping(payload_json: str | None) -> dict[str, Any]:
    """Parse a JSON object payload; empty / null means an empty object."""
    value = loads_json(payload_json)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object.")
    return value


def timeout_opt(seconds: int) -> int | None:
    """Map SiLA timeout sentinel: 0 means omit."""
    return None if seconds == 0 else seconds


def index_opt(value: int) -> int | None:
    """Map SiLA experiment-index sentinel: 0 means omit."""
    return None if value == 0 else value


def queue_type_opt(value: str) -> str | None:
    """Map SiLA queue-type sentinel: empty/whitespace means omit."""
    stripped = (value or "").strip()
    return stripped or None


def bool_opt(value: str) -> bool | None:
    """Map SiLA boolean sentinel: empty/whitespace means omit."""
    stripped = (value or "").strip().lower()
    if not stripped:
        return None
    if stripped in ("true", "false"):
        return stripped == "true"
    raise ValueError(f"Expected '', 'true', or 'false'; got {value!r}")


def sniff_bearer_token(obj: Any) -> str | None:
    """Best-effort token extraction after login/refresh responses.

    Walks nested dict/list structures once looking for a string value whose
    key contains ``token`` (case-insensitive).
    """
    if isinstance(obj, str) and obj:
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "token" in str(k).lower() and isinstance(v, str) and v:
                return v
        for v in obj.values():
            got = sniff_bearer_token(v)
            if got:
                return got
    elif isinstance(obj, list):
        for item in obj:
            got = sniff_bearer_token(item)
            if got:
                return got
    return None
