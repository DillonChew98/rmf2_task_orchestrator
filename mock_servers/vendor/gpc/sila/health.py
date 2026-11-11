"""Background readiness and dashboard status polling for WinGPC."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import Any, Callable

from gpc_automation.models import InstrumentSnapshot
from sila_common.equipment_status import PARAMETERS
from sila_common.status_http import CachingHealthPoller, ReachabilityCache

from .executor import SKIPPED
from .state_machine import GpcState, GpcStateMachine

_log = logging.getLogger(__name__)

_BUSY_STATES = frozenset({GpcState.RUN, GpcState.PAUSE, GpcState.MAINTENANCE})


def _activity(system_status: str, analysis_status: str, state: GpcState) -> str:
    if system_status == "red" or analysis_status == "red":
        return "Error"
    if state in _BUSY_STATES:
        return "Busy"
    return "Idle"


class GpcStatusPoller(CachingHealthPoller):
    """Sample WinGPC only when the UI executor is free and the FSM is connected."""

    def __init__(
        self,
        cache: ReachabilityCache,
        *,
        fsm: GpcStateMachine,
        submit_fn: Callable[[], Any],
        interval_s: float,
        timeout_s: float,
        on_snapshot: Callable[[InstrumentSnapshot], None] | None = None,
    ) -> None:
        self._fsm = fsm
        self._submit_fn = submit_fn
        self._timeout_s = timeout_s
        self._on_snapshot = on_snapshot
        self._pending: Future[InstrumentSnapshot] | None = None
        self._pending_lock = threading.Lock()
        cache.configure("gpc", PARAMETERS["gpc"])
        super().__init__(cache, lambda: True, interval_s=interval_s, name="gpc-health")

    def probe(self) -> bool:
        if self._fsm.state == GpcState.DISCONNECTED:
            self._cache.record(False)
            return False

        with self._pending_lock:
            pending = self._pending
        if pending is not None:
            if not pending.done():
                self._cache.skip()
                return self._cache.get() is True
            with self._pending_lock:
                if self._pending is pending:
                    self._pending = None
            return self._consume(pending, raise_error=True)

        submitted = self._submit_fn()
        if submitted is SKIPPED:
            self._cache.skip()
            return self._cache.get() is True
        if not isinstance(submitted, Future):
            return self._store_snapshot(submitted)

        try:
            snapshot = submitted.result(timeout=self._timeout_s)
        except TimeoutError:
            with self._pending_lock:
                self._pending = submitted
            submitted.add_done_callback(self._on_late)
            with self._pending_lock:
                still_pending = self._pending is submitted
            if not still_pending:
                return self._cache.get() is True
            self._cache.skip()
            return self._cache.get() is True
        except Exception:
            self._cache.record(False)
            raise
        return self._store_snapshot(snapshot)

    def _on_late(self, fut: Future[InstrumentSnapshot]) -> None:
        with self._pending_lock:
            if self._pending is not fut:
                return
            self._pending = None
        try:
            self._consume(fut, raise_error=False)
        except Exception:
            _log.exception("GPC status probe failed after the wait budget")

    def _consume(self, fut: Future[InstrumentSnapshot], *, raise_error: bool) -> bool:
        try:
            snapshot = fut.result(timeout=0)
        except Exception:
            self._cache.record(False)
            if raise_error:
                raise
            return False
        return self._store_snapshot(snapshot)

    def _store_snapshot(self, snapshot: InstrumentSnapshot) -> bool:
        activity = _activity(
            snapshot.system_status,
            snapshot.analysis_status,
            self._fsm.state,
        )
        self._cache.record(True)
        self._cache.record_parameters(
            {
                "system_status": snapshot.system_status,
                "analysis_status": snapshot.analysis_status,
            },
            activity,
        )
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(snapshot)
            except Exception:
                _log.exception("GPC status on_snapshot callback failed")
        return True
