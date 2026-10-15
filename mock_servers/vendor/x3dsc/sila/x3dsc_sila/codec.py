"""TRIOS REST JSON helpers for SiLA command/property payloads.

Thin re-export of ``sila_common.codec``; see that module for the implementation.
"""

from __future__ import annotations

from typing import Any

from sila_common.codec import (
    bool_opt,
    index_opt,
    loads_mapping,
    queue_type_opt,
    sniff_bearer_token,
    timeout_opt,
)
from sila_common.codec import dumps_json as _dumps_json

__all__ = [
    "bool_opt",
    "dumps_json",
    "index_opt",
    "loads_mapping",
    "queue_type_opt",
    "sniff_bearer_token",
    "timeout_opt",
]


def dumps_json(data: Any) -> str:
    """Serialize API JSON values to UTF-8 string for SiLA String responses (plain JSON)."""
    return _dumps_json(data, numpy=False)
