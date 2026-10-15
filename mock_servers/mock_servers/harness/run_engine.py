"""A run that takes real wall-clock time, shared by all four mock instruments.

State is *derived from a deadline*, never mutated by a background timer. A reader
takes the lock, notices the deadline has passed, and settles Running -> Complete
itself before returning. Whichever thread reads first does the transition, exactly
once, atomically.

That matters because sila2 dispatches every SiLA command on a gRPC thread pool, so
several threads read this state while others mutate it. Deriving from a deadline
means there is no window in which a reader sees Running with elapsed > duration,
there is no thread to leak or join, and a test can inject a fake clock and drive
time by hand instead of sleeping.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class RunState(str, Enum):
    """The value is the wire form, so it can go straight into a JSON payload."""

    IDLE = "Idle"
    QUEUED = "Queued"
    RUNNING = "Running"
    COMPLETE = "Complete"
    ABORTED = "Aborted"


SETTLED = frozenset({RunState.IDLE, RunState.COMPLETE, RunState.ABORTED})


class IllegalTransition(RuntimeError):
    """Raised when a caller asks for something the instrument's state forbids.

    Each mock maps this onto whatever its instrument really raises -- an HTTP 409
    for the TRIOS instruments, a gpc.state.invalid rejection for GPC.
    """


@dataclass(frozen=True)
class RunSnapshot:
    state: RunState
    elapsed: float
    remaining: float
    progress: float
    experiment_id: str | None


class TimedRun:
    def __init__(
        self,
        run_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_settle: Callable[[RunState], None] | None = None,
    ) -> None:
        self.run_seconds = float(run_seconds)
        self._clock = clock
        self._on_settle = on_settle
        self._cond = threading.Condition(threading.RLock())
        self._state = RunState.IDLE
        self._t0 = 0.0
        self._deadline = 0.0
        self._experiment_id: str | None = None
        self._watchdog: threading.Timer | None = None

    # -- reads ------------------------------------------------------------

    def snapshot(self) -> RunSnapshot:
        with self._cond:
            settled = self._settle_locked()
            now = self._clock()
            if self._state is RunState.RUNNING:
                elapsed = now - self._t0
                remaining = max(0.0, self._deadline - now)
                progress = min(1.0, elapsed / self.run_seconds) if self.run_seconds else 1.0
            else:
                elapsed = 0.0 if self._state in (RunState.IDLE, RunState.QUEUED) else self.run_seconds
                remaining = 0.0
                progress = 1.0 if self._state is RunState.COMPLETE else 0.0
            snap = RunSnapshot(self._state, elapsed, remaining, progress, self._experiment_id)
        if settled is not None:
            self._fire_settle(settled)
        return snap

    @property
    def state(self) -> RunState:
        return self.snapshot().state

    def is_running(self) -> bool:
        return self.state is RunState.RUNNING

    # -- writes -----------------------------------------------------------

    def queue(self, experiment_id: str) -> None:
        with self._cond:
            self._settle_locked()
            if self._state is RunState.RUNNING:
                raise IllegalTransition("a run is already in progress")
            self._experiment_id = experiment_id
            self._state = RunState.QUEUED
            self._cond.notify_all()

    def start(self, *, experiment_id: str | None = None, force: bool = False) -> None:
        settled = None
        with self._cond:
            settled = self._settle_locked()
            if self._state is RunState.RUNNING and not force:
                raise IllegalTransition("a run is already in progress")
            now = self._clock()
            if experiment_id is not None:
                self._experiment_id = experiment_id
            self._state = RunState.RUNNING
            self._t0 = now
            self._deadline = now + self.run_seconds
            self._cond.notify_all()
        if settled is not None:
            self._fire_settle(settled)
        self._arm_watchdog()

    def abort(self) -> None:
        with self._cond:
            self._settle_locked()
            if self._state is not RunState.RUNNING:
                raise IllegalTransition("nothing is running")
            self._state = RunState.ABORTED
            self._cond.notify_all()
        self._fire_settle(RunState.ABORTED)

    def reset(self) -> None:
        self._cancel_watchdog()
        with self._cond:
            self._state = RunState.IDLE
            self._experiment_id = None
            self._t0 = self._deadline = 0.0
            self._cond.notify_all()

    # -- blocking waits ---------------------------------------------------

    def wait_until_idle(self, timeout: float | None) -> bool:
        """Block until no run is in progress. False if the timeout ran out first."""
        return self._wait_for(lambda: self._state in SETTLED, timeout)

    def wait_for_start(self, timeout: float | None) -> bool:
        """Block until a run is in progress. False if the timeout ran out first."""
        return self._wait_for(lambda: self._state is RunState.RUNNING, timeout)

    def _wait_for(self, predicate: Callable[[], bool], timeout: float | None) -> bool:
        give_up = None if timeout is None else self._clock() + timeout
        settled = None
        with self._cond:
            while True:
                settled = self._settle_locked() or settled
                if predicate():
                    break
                slice_s = self._wait_slice_locked(give_up)
                if slice_s is not None and slice_s <= 0:
                    break
                self._cond.wait(slice_s)
            result = predicate()
        if settled is not None:
            self._fire_settle(settled)
        return result

    def _wait_slice_locked(self, give_up: float | None) -> float | None:
        """How long to sleep: until the run's deadline, or the caller's, whichever first.

        Waking at the deadline is what lets a waiter settle the run itself when no
        other thread happens to read.
        """
        now = self._clock()
        candidates = []
        if give_up is not None:
            candidates.append(give_up - now)
        if self._state is RunState.RUNNING:
            candidates.append(max(0.0, self._deadline - now))
        if not candidates:
            return None
        return min(candidates)

    # -- internals --------------------------------------------------------

    def _settle_locked(self) -> RunState | None:
        """Flip Running -> Complete if the deadline has passed. Caller holds the lock.

        Returns the state settled into, or None if nothing changed, so the caller
        can fire on_settle *outside* the lock. Firing it inside would call
        update_<Property>, which fans out to sila2 subscriber queues -- a lock
        inversion waiting to happen.
        """
        if self._state is RunState.RUNNING and self._clock() >= self._deadline:
            self._state = RunState.COMPLETE
            self._cond.notify_all()
            return RunState.COMPLETE
        return None

    def _fire_settle(self, state: RunState) -> None:
        self._cancel_watchdog()
        if self._on_settle is not None:
            self._on_settle(state)

    # -- the settle watchdog ---------------------------------------------
    #
    # Settling lazily is right for reads: whoever looks first does the work, with
    # no thread to leak and no window where a reader sees stale state. But a
    # caller that wants to be TOLD when a run ends cannot be served that way --
    # with nobody reading, nothing settles and on_settle never fires.
    #
    # That is not hypothetical. GPC's server returns its state machine to Connect
    # from on_settle. Driven through the backend REST API, nothing polls the run,
    # so the dashboard sat at "Run" forever after the sequence had finished.
    #
    # So: when there IS an on_settle, arm a one-shot timer for the deadline whose
    # only job is to read. The read settles through the same locked path as any
    # other, so this adds a nudge, not a second way to change state.

    def _arm_watchdog(self) -> None:
        if self._on_settle is None:
            return
        self._cancel_watchdog()
        delay = max(0.0, self._deadline - self._clock())
        timer = threading.Timer(delay + 0.05, self.snapshot)
        timer.daemon = True
        with self._cond:
            self._watchdog = timer
        timer.start()

    def _cancel_watchdog(self) -> None:
        with self._cond:
            timer, self._watchdog = self._watchdog, None
        if timer is not None:
            timer.cancel()
