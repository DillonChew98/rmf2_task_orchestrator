"""Single-thread executor owning the FtirAutomation OPUS session.

All OPUS calls are serialized through this executor so the opuspy_api HTTP
socket is only used from one thread, regardless of how many SiLA gRPC worker
threads dispatch commands concurrently. Instrument-touching commands reuse
one persistent OPUS HTTP session (connect once, disconnect on Disconnect or
server shutdown).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, NoReturn, TypeVar

from ftir_automation import FtirAutomation
from ftir_automation.health import SKIPPED
from ftir_automation.models.instrument import InstrumentSnapshot
from ftir_automation.telemetry import get_telemetry, sc

log = logging.getLogger(__name__)

T = TypeVar("T")

STATUS_IDLE = "Idle"
STATUS_BUSY = "Busy"
STATUS_ERROR = "Error"
STATUS_UNREACHABLE = "Unreachable"

NOT_CONNECTED_MSG = (
    "Failed to open an OPUS HTTP session. Ensure OPUS is running with "
    "/HTTPSERVER=On and the configured host/port are reachable."
)


class FtirExecutor:
    """Single-thread executor that owns the FtirAutomation instance."""

    def __init__(self, ftir: FtirAutomation) -> None:
        self.ftir = ftir
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ftir-opus")
        self._lock = threading.Lock()
        self._in_flight = 0
        self._status = STATUS_IDLE
        self._status_listeners: list[Callable[[str], None]] = []

    @property
    def connected(self) -> bool:
        return self.ftir._opus_api is not None  # noqa: SLF001 — same package family

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def set_status_listener(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked on every status transition (may be re-entrant)."""
        with self._lock:
            self._status_listeners.append(callback)

    def _notify_status(self, status: str) -> None:
        with self._lock:
            if self._status == status:
                return
            self._status = status
            listeners = list(self._status_listeners)
        for cb in listeners:
            try:
                cb(status)
            except Exception:
                log.exception("instrument status listener failed")

    def _begin(self) -> None:
        with self._lock:
            self._in_flight += 1

    def _end(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def run(
        self,
        fn: Callable[..., T],
        *args: Any,
        report_busy: bool = True,
        settle: bool = True,
        **kwargs: Any,
    ) -> T:
        if report_busy:
            self._notify_status(STATUS_BUSY)
        self._begin()
        try:
            result = self._pool.submit(fn, *args, **kwargs).result()
            if settle:
                self._notify_status(STATUS_IDLE)
            return result
        except Exception:
            if settle:
                self._notify_status(STATUS_ERROR)
            raise
        finally:
            self._end()

    def try_run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | object:
        """Non-blocking submit. Returns ``SKIPPED`` if a command is in flight.

        Quiet path: never reports Busy (used by health probes).
        """
        with self._lock:
            if self._in_flight > 0:
                return SKIPPED
            self._in_flight += 1
        try:
            result = self._pool.submit(fn, *args, **kwargs).result()
            with self._lock:
                cur = self._status
            if cur in (STATUS_UNREACHABLE, STATUS_ERROR):
                self._notify_status(STATUS_IDLE)
            return result
        except Exception as exc:
            if isinstance(exc, (ConnectionError, OSError, ImportError)):
                self._notify_status(STATUS_UNREACHABLE)
            raise
        finally:
            self._end()

    def _session_work(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self.ftir._opus_api is None:  # noqa: SLF001 — ensure persistent session
            self.ftir.connect()  # may raise ConnectionError / ImportError
        try:
            return fn(*args, **kwargs)
        except (ConnectionError, OSError, ImportError):
            try:
                self.ftir.disconnect()
            except Exception:
                log.exception("disconnect after session failure failed")
            raise

    def try_get_snapshot(self) -> InstrumentSnapshot | object:
        """Quiet snapshot read; returns ``SKIPPED`` when a command is in flight."""

        def _work() -> InstrumentSnapshot:
            if self.ftir._opus_api is None:  # noqa: SLF001
                self.ftir.connect()
            return self.ftir.get_snapshot()

        return self.try_run(_work)

    def run_with_session(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Ensure OPUS is connected, run *fn*, leave the session open."""
        tel = get_telemetry()
        session_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        span_cm, span = tel.start_span(
            "opus.session",
            **{
                sc.ATTR_OPUS_SESSION_ID: session_id,
                sc.ATTR_OPUS_HOST: self.ftir._opus_ip,  # noqa: SLF001
                sc.ATTR_OPUS_PORT: self.ftir._opus_port,  # noqa: SLF001
            },
        )
        ok = False
        reason = ""
        self._notify_status(STATUS_BUSY)
        try:
            result = self.run(
                self._session_work, fn, *args, report_busy=False, settle=False, **kwargs
            )
            ok = True
            self._notify_status(STATUS_IDLE)
            return result
        except Exception as exc:
            reason = _session_failure_reason(exc)
            tel.set_span_error(span, str(exc), outcome=reason)
            if isinstance(exc, (ConnectionError, OSError, ImportError)):
                self._notify_status(STATUS_UNREACHABLE)
            else:
                self._notify_status(STATUS_ERROR)
            raise
        finally:
            tel.record_session(time.perf_counter() - started, ok=ok, reason=reason)
            span_cm.__exit__(None, None, None)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
        try:
            self.ftir.disconnect()
        except Exception:
            log.exception("disconnect during executor shutdown failed")


def _session_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        return sc.REASON_IMPORT_ERROR
    if isinstance(exc, (ConnectionError, OSError)):
        return sc.REASON_CONNECTION_REFUSED
    return sc.REASON_OPUS_ERROR


def _raise_session_error(
    exc: BaseException,
    *,
    not_connected: Callable[[str], BaseException],
    opus_api: Callable[[str], BaseException],
) -> NoReturn:
    """Map Python connect/work failures to SiLA DefinedExecutionErrors."""
    from ftir_automation.telemetry import mark_current_span_error

    mark_current_span_error(str(exc), outcome=sc.OUTCOME_ERROR)
    if isinstance(exc, (ConnectionError, ImportError)):
        raise not_connected(str(exc) or NOT_CONNECTED_MSG) from exc
    raise opus_api(str(exc)) from exc
