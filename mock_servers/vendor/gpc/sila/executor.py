"""Single-thread UIA executor for GpcAutomation."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from gpc_automation import GpcAutomation
from gpc_automation.models import InstrumentSnapshot

T = TypeVar("T")

# Returned by try_submit when a SiLA command already owns the UI thread.
SKIPPED = object()


class GpcExecutor:
    """
    Single-thread executor that owns the `GpcAutomation` instance.

    All UIA calls are routed through this executor so that pywinauto / COM
    objects are only touched from one thread, regardless of how many SiLA
    gRPC worker threads are dispatching commands concurrently.
    """

    def __init__(self, gpc: GpcAutomation) -> None:
        self.gpc = gpc
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpc-uia")
        self._lock = threading.Lock()
        self._in_flight = 0

    def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            self._in_flight += 1
        try:
            return self._pool.submit(fn, *args, **kwargs).result()
        finally:
            with self._lock:
                self._in_flight -= 1

    def try_submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T] | object:
        """Queue *fn* without waiting. Returns ``SKIPPED`` if a command is in flight.

        ``_in_flight`` drops only after *fn* returns, so a caller that stops
        waiting must not treat that as the work having finished.
        """
        with self._lock:
            if self._in_flight > 0:
                return SKIPPED
            self._in_flight += 1
        try:
            future = self._pool.submit(fn, *args, **kwargs)
        except Exception:
            with self._lock:
                self._in_flight -= 1
            raise

        def _release(done: Future[T]) -> None:
            try:
                done.result()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._in_flight -= 1

        future.add_done_callback(_release)
        return future

    def try_submit_snapshot(self) -> Future[InstrumentSnapshot] | object:
        """Quiet snapshot submit; returns ``SKIPPED`` when a command is in flight."""
        return self.try_submit(self.gpc.get_instrument_snapshot)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
