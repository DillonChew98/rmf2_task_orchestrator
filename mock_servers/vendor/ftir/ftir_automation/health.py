"""Contention-aware FTIR health poller.

Submits through the SiLA ``FtirExecutor`` via a non-blocking ``try_run``
callback so a 60 s probe never queues behind an in-flight measurement.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

try:
    from lab_observability.health import SKIPPED, HealthPoller
except ImportError:  # pragma: no cover
    SKIPPED = object()

    class HealthPoller:  # type: ignore[no-redef]
        def __init__(self, *, interval_s: float = 60.0, name: str = "health") -> None:
            self.interval_s = interval_s
            self.name = name

        def start(self) -> None:
            return None

        def stop(self, timeout: float = 5.0) -> None:
            return None

from ftir_automation.telemetry import get_telemetry

_log = logging.getLogger(__name__)

TryRun = Callable[..., Any]


class FtirHealthPoller(HealthPoller):
    """Sets ``ftir.instrument.up`` and re-emits cached OPUS health gauges."""

    def __init__(
        self,
        try_run: TryRun,
        probe_fn: Callable[[], Any],
        *,
        interval_s: float = 60.0,
    ) -> None:
        super().__init__(interval_s=interval_s, name="ftir-health")
        self._try_run = try_run
        self._probe_fn = probe_fn
        self._tel = get_telemetry()

    def probe(self) -> Any:
        try:
            result = self._try_run(self._probe_fn)
        except Exception:
            self._tel.set_instrument_up(False)
            raise
        if result is SKIPPED:
            self._tel.record_probe_skipped()
            _log.debug("FTIR health probe skipped (executor busy)")
            return SKIPPED
        up = bool(result)
        self._tel.set_instrument_up(up)
        return up


def snapshot_is_up(snapshot: Optional[Any]) -> bool:
    if snapshot is None:
        return False
    return bool(getattr(snapshot, "connected", False))
