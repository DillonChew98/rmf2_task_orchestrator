"""Cached, read-only dashboard card samples from instrument-local clients."""

from __future__ import annotations

import logging
import math
from typing import Any

from .status_http import CachingHealthPoller, ReachabilityCache

_log = logging.getLogger(__name__)

# IDs, labels and units match observability/manifests/{kind}.yml.
PARAMETERS = {
    "ftir": {"instrument_up": ("Instrument", "")},
    "sdt": {
        "temperature": ("Temperature", "degC"), "weight": ("Weight", "mg"),
        "furnace_open": ("Furnace open", ""), "gas_flow": ("Gas flow", "mL/min"),
    },
    "x3dsc": {
        "temperature": ("Temperature", "°C"), "arm_position": ("Arm position", ""),
        "lid_state": ("Lid", ""), "is_moving": ("Moving", ""),
        "is_online": ("Online", ""), "is_enabled": ("Enabled", ""),
    },
    "gpc": {
        "system_status": ("System", ""),
        "analysis_status": ("Analysis", ""),
    },
}

SOURCES = {
    "sdt": {
        "get_instrument_status": {"temperature": "currentTemperature", "weight": "currentWeight"},
        "get_furnace_status": {"furnace_open": "isOpen"},
        "get_gas_status": {"gas_flow": "flowRateMlPerMin"},
    },
    "x3dsc": {
        "get_instrument_status": {"temperature": "currentTemperature"},
        "get_autosampler_status": {
            "arm_position": "armPosition", "lid_state": "lidState", "is_moving": "isMoving",
            "is_online": "isOnline", "is_enabled": "isEnabled",
        },
    },
}


def _payload(raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("success") is False:
        return {}
    data = raw.get("data", raw)
    return data if isinstance(data, dict) else {}


def _value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else None


def _activity(status: dict, kind: str) -> str | None:
    if not status:
        return None
    if status.get("hasErrors") is True:
        return "Error"
    if (kind == "sdt" and status.get("instrumentIsBusy") is True
            or kind == "x3dsc" and status.get("autosamplerInMotion") is True):
        return "Busy"
    procedure = status.get("procedureState")
    if procedure is None:
        return None
    return "Idle" if procedure == "Idle" else "Busy"


class TriosStatusPoller(CachingHealthPoller):
    """Preserve readiness semantics; independently collect optional card sources."""

    def __init__(self, client: Any, cache: ReachabilityCache, *, kind: str,
                 interval_s: float, timeout_s: float) -> None:
        self._client = client
        self._kind = kind
        self._timeout_s = timeout_s
        cache.configure(kind, PARAMETERS[kind])
        super().__init__(cache, self._reachable, interval_s=interval_s, name=f"{kind}-health")

    def _reachable(self) -> bool:
        self._client.get_health(request_timeout=self._timeout_s)
        return True

    def probe(self) -> bool:
        up = super().probe()
        values = {}
        status = {}
        for method, fields in SOURCES[self._kind].items():
            try:
                payload = _payload(getattr(self._client, method)(request_timeout=self._timeout_s))
            except Exception:
                _log.warning("%s status source %s failed", self._kind, method, exc_info=True)
                payload = {}
            if method == "get_instrument_status":
                status = payload
            values.update({key: _value(payload.get(field)) for key, field in fields.items()})
        self._cache.record_parameters(values, _activity(status, self._kind))
        return up
