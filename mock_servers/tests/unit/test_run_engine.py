"""TimedRun tests.

The arithmetic is tested on an injected clock so nothing sleeps and nothing is
flaky. The blocking waits are tested on the real clock at sub-second durations,
because that is the part an injected clock cannot prove.
"""

from __future__ import annotations

import threading
import time

import pytest

from mock_servers.harness.run_engine import (
    IllegalTransition,
    RunState,
    TimedRun,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# -- state arithmetic, on a fake clock ------------------------------------


def test_starts_idle(clock):
    run = TimedRun(20.0, clock=clock)
    assert run.state is RunState.IDLE
    assert not run.is_running()


def test_run_is_running_until_the_deadline(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    assert run.state is RunState.RUNNING

    clock.advance(19.9)
    assert run.state is RunState.RUNNING

    clock.advance(0.1)
    assert run.state is RunState.COMPLETE


def test_snapshot_reports_progress(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    clock.advance(5.0)

    snap = run.snapshot()
    assert snap.state is RunState.RUNNING
    assert snap.elapsed == pytest.approx(5.0)
    assert snap.remaining == pytest.approx(15.0)
    assert snap.progress == pytest.approx(0.25)


def test_second_start_while_running_is_refused(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    with pytest.raises(IllegalTransition):
        run.start()


def test_force_restarts_a_running_run(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    clock.advance(10.0)
    run.start(force=True)
    assert run.snapshot().elapsed == pytest.approx(0.0)


def test_start_after_completion_is_allowed(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    clock.advance(20.0)
    assert run.state is RunState.COMPLETE
    run.start()
    assert run.state is RunState.RUNNING


def test_abort_stops_a_run(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    clock.advance(5.0)
    run.abort()
    assert run.state is RunState.ABORTED


def test_abort_with_nothing_running_is_refused(clock):
    run = TimedRun(20.0, clock=clock)
    with pytest.raises(IllegalTransition):
        run.abort()


def test_abort_after_completion_is_refused(clock):
    run = TimedRun(20.0, clock=clock)
    run.start()
    clock.advance(20.0)
    with pytest.raises(IllegalTransition):
        run.abort()


def test_queue_records_the_experiment(clock):
    run = TimedRun(20.0, clock=clock)
    run.queue("exp-1")
    assert run.snapshot().state is RunState.QUEUED
    assert run.snapshot().experiment_id == "exp-1"


def test_reset_returns_to_idle(clock):
    run = TimedRun(20.0, clock=clock)
    run.start(experiment_id="exp-1")
    run.reset()
    snap = run.snapshot()
    assert snap.state is RunState.IDLE
    assert snap.experiment_id is None


# -- settling ------------------------------------------------------------


def test_on_settle_fires_once(clock):
    fired: list[RunState] = []
    run = TimedRun(20.0, clock=clock, on_settle=fired.append)
    run.start()
    clock.advance(20.0)

    run.snapshot()
    run.snapshot()
    run.snapshot()

    assert fired == [RunState.COMPLETE]


def test_on_settle_fires_for_abort(clock):
    fired: list[RunState] = []
    run = TimedRun(20.0, clock=clock, on_settle=fired.append)
    run.start()
    run.abort()
    assert fired == [RunState.ABORTED]


def test_on_settle_runs_outside_the_lock(clock):
    """The callback must be able to re-enter TimedRun without deadlocking.

    Real callers do exactly this: GPC's on_settle pushes a state change that a
    subscriber thread immediately reads back.
    """
    seen: list[RunState] = []
    run = TimedRun(20.0, clock=clock, on_settle=lambda s: seen.append(run.state))
    run.start()
    clock.advance(20.0)
    run.snapshot()
    assert seen == [RunState.COMPLETE]


# -- blocking waits, on the real clock ------------------------------------


def test_wait_until_idle_returns_immediately_when_idle():
    run = TimedRun(20.0)
    t0 = time.monotonic()
    assert run.wait_until_idle(5.0) is True
    assert time.monotonic() - t0 < 0.1


def test_wait_until_idle_blocks_for_the_run_then_returns():
    run = TimedRun(0.4)
    run.start()
    t0 = time.monotonic()
    assert run.wait_until_idle(5.0) is True
    elapsed = time.monotonic() - t0
    assert 0.3 < elapsed < 1.5
    assert run.state is RunState.COMPLETE


def test_wait_until_idle_times_out_and_says_so():
    run = TimedRun(10.0)
    run.start()
    t0 = time.monotonic()
    assert run.wait_until_idle(0.3) is False
    elapsed = time.monotonic() - t0
    assert 0.2 < elapsed < 1.0
    assert run.state is RunState.RUNNING


def test_wait_for_start_wakes_when_another_thread_starts():
    run = TimedRun(5.0)

    def start_soon() -> None:
        time.sleep(0.2)
        run.start()

    threading.Thread(target=start_soon, daemon=True).start()
    t0 = time.monotonic()
    assert run.wait_for_start(5.0) is True
    assert time.monotonic() - t0 < 2.0


def test_wait_until_idle_wakes_on_abort():
    run = TimedRun(30.0)
    run.start()

    def abort_soon() -> None:
        time.sleep(0.2)
        run.abort()

    threading.Thread(target=abort_soon, daemon=True).start()
    t0 = time.monotonic()
    assert run.wait_until_idle(5.0) is True
    assert time.monotonic() - t0 < 2.0
    assert run.state is RunState.ABORTED


# -- concurrency ---------------------------------------------------------


def test_many_readers_during_a_run_see_a_consistent_snapshot():
    """No reader may ever see Running with elapsed past the duration."""
    run = TimedRun(0.5)
    run.start()
    bad: list[str] = []

    def reader() -> None:
        for _ in range(400):
            snap = run.snapshot()
            if snap.state is RunState.RUNNING and snap.elapsed > run.run_seconds:
                bad.append(f"{snap.state} elapsed={snap.elapsed}")

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert bad == []


def test_only_one_thread_wins_the_start_race():
    """Exactly one concurrent start() succeeds; the rest are refused."""
    run = TimedRun(5.0)
    wins: list[int] = []
    barrier = threading.Barrier(8)

    def racer(i: int) -> None:
        barrier.wait()
        try:
            run.start()
            wins.append(i)
        except IllegalTransition:
            pass

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(wins) == 1


# -- the settle watchdog -------------------------------------------------


def test_on_settle_fires_with_nobody_reading():
    """The gap the backend found: a run must announce its own end.

    Lazy settling needs a reader. With on_settle set, TimedRun arms a timer whose
    only job is to read, so a caller that subscribes rather than polls still hears
    about the end of the run.
    """
    fired = threading.Event()
    run = TimedRun(0.3, on_settle=lambda _state: fired.set())
    run.start()

    assert fired.wait(5.0), "on_settle never fired without a reader"
    assert run.state is RunState.COMPLETE


def test_watchdog_does_not_fire_after_an_abort():
    seen: list[RunState] = []
    run = TimedRun(0.4, on_settle=seen.append)
    run.start()
    run.abort()

    time.sleep(0.8)
    assert seen == [RunState.ABORTED], "the watchdog should not settle an aborted run"


def test_restarting_replaces_the_watchdog_rather_than_stacking():
    seen: list[RunState] = []
    run = TimedRun(0.4, on_settle=seen.append)
    run.start()
    run.start(force=True)
    run.start(force=True)

    time.sleep(1.2)
    assert seen == [RunState.COMPLETE], f"expected one settle, got {seen}"


def test_no_watchdog_thread_without_a_callback():
    """Only callers that need the nudge pay for a timer."""
    before = threading.active_count()
    run = TimedRun(30.0)
    run.start()
    assert threading.active_count() == before
