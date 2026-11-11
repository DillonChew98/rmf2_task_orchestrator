"""Loki curve lines for a completed X3 DSC export.

Arrays stay on log lines. They never enter Prometheus.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)
_curve_log = logging.getLogger("x3dsc.curve")


def emit_curves(docs: list[dict[str, Any]]) -> int:
    """Emit one ``x3dsc.curve`` JSON line per channel. Returns the line count."""
    emitted = 0
    for doc in docs:
        if not doc.get("run_id"):
            continue
        payload = dict(doc)
        payload["event"] = "x3dsc.curve"
        _curve_log.info("%s", json.dumps(payload, separators=(",", ":")))
        emitted += 1
    if emitted:
        _log.info("emitted %d x3dsc.curve lines", emitted)
    return emitted
