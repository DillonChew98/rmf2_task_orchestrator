"""Contention-aware FTIR health poller.

Submits through the SiLA ``FtirExecutor`` via a non-blocking ``try_run``
callback so a 60 s probe never queues behind an in-flight measurement.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
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
SubmitFn = Callable[[], Any]


class FtirHealthPoller(HealthPoller):
    """Sets ``ftir.instrument.up`` and re-emits cached OPUS health gauges."""

    def __init__(
        self,
        try_run: TryRun | None = None,
        probe_fn: Callable[[], Any] | None = None,
        *,
        interval_s: float = 60.0,
        cache: Any = None,
        timeout_s: float = 3.0,
        submit_fn: SubmitFn | None = None,
        activity_fn: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(interval_s=interval_s, name="ftir-health")
        self._try_run = try_run
        self._probe_fn = probe_fn
        self._cache = cache
        self._timeout_s = timeout_s
        self._submit_fn = submit_fn
        self._activity_fn = activity_fn
        self._pending: Future[Any] | None = None
        self._pending_lock = threading.Lock()
        self._tel = get_telemetry()

    def probe(self) -> Any:
        if self._submit_fn is not None:
            return self._probe_submitted()
        if self._try_run is None or self._probe_fn is None:
            raise RuntimeError("FTIR health poller has no probe")
        try:
            result = self._try_run(self._probe_fn)
        except Exception:
            self._store(False)
            raise
        return self._store_result(result)

    def _probe_submitted(self) -> Any:
        with self._pending_lock:
            pending = self._pending
        if pending is not None:
            if not pending.done():
                return self._note_skip()
            with self._pending_lock:
                if self._pending is pending:
                    self._pending = None
            return self._consume(pending, raise_error=True)

        assert self._submit_fn is not None
        submitted = self._submit_fn()
        if submitted is SKIPPED:
            return self._note_skip()
        if not isinstance(submitted, Future):
            return self._store_result(submitted)
        try:
            submitted.result(timeout=self._timeout_s)
        except TimeoutError:
            with self._pending_lock:
                self._pending = submitted
            submitted.add_done_callback(self._on_late)
            with self._pending_lock:
                still_pending = self._pending is submitted
            if not still_pending:
                return SKIPPED
            return self._note_skip()
        except Exception:
            return self._consume(submitted, raise_error=True)
        return self._consume(submitted, raise_error=True)

    def _on_late(self, fut: Future[Any]) -> None:
        with self._pending_lock:
            if self._pending is not fut:
                return
            self._pending = None
        try:
            self._consume(fut, raise_error=False)
        except Exception:
            _log.exception("FTIR health probe failed after the wait budget")

    def _consume(self, fut: Future[Any], *, raise_error: bool) -> Any:
        try:
            result = fut.result(timeout=0)
        except Exception:
            self._store(False)
            if raise_error:
                raise
            return False
        return self._store_result(result)

    def _store_result(self, result: Any) -> Any:
        if result is SKIPPED:
            return self._note_skip()
        up = snapshot_is_up(result) if hasattr(result, "connected") else bool(result)
        self._store(up)
        return up

    def _store(self, up: bool) -> None:
        self._tel.set_instrument_up(up)
        if self._cache is not None:
            self._cache.record(up)
            if up and self._activity_fn is not None:
                self._cache.record_activity(self._activity_fn())

    def _note_skip(self) -> object:
        self._tel.record_probe_skipped()
        if self._cache is not None:
            self._cache.skip()
        _log.debug("FTIR health probe skipped (executor busy)")
        return SKIPPED


def snapshot_is_up(snapshot: Optional[Any]) -> bool:
    if snapshot is None:
        return False
    return bool(getattr(snapshot, "connected", False))
