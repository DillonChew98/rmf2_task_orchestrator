"""JSON helpers for SiLA String payloads carrying OPUS data.

Thin re-export of ``sila_common.codec`` with numpy array/scalar encoding enabled.
"""

from __future__ import annotations

from typing import Any

from sila_common.codec import loads_mapping
from sila_common.codec import dumps_json as _dumps_json

__all__ = ["dumps_json", "loads_mapping"]


def dumps_json(data: Any) -> str:
    """Serialize a value to a compact UTF-8 JSON string for SiLA String responses."""
    return _dumps_json(data, numpy=True)
