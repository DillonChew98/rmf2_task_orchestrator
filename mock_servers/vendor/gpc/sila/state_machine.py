"""
GPC server state chart.

Single-region, thread-safe FSM enforcing the legal command set in each state:

    Disconnected --(Connect)--> Connect
    Connect --(Disconnect)--> Disconnected
    Connect --(ClickStart toggles ON)--> Run
    Run --(ClickStart toggles OFF / Stop)--> Connect
    Run --(Pause)--> Pause
    Pause --(Resume)--> Run
    Pause --(Stop)--> Connect
    Connect --(EnterMaintenance)--> Maintenance
    Maintenance --(ExitMaintenance)--> Connect

State changes are pushed to a caller-supplied callback so the SiLA observable
property `InstrumentLifecycle.CurrentState` can re-emit them to subscribers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from enum import Enum

log = logging.getLogger(__name__)


class GpcState(str, Enum):
    """States exposed via SiLA. The string value is the wire form."""

    DISCONNECTED = "Disconnected"
    CONNECT = "Connect"
    RUN = "Run"
    PAUSE = "Pause"
    MAINTENANCE = "Maintenance"


# Allowed direct transitions: source -> {targets}.
_ALLOWED: dict[GpcState, frozenset[GpcState]] = {
    GpcState.DISCONNECTED: frozenset({GpcState.CONNECT}),
    GpcState.CONNECT: frozenset(
        {GpcState.DISCONNECTED, GpcState.RUN, GpcState.MAINTENANCE}
    ),
    GpcState.RUN: frozenset({GpcState.CONNECT, GpcState.PAUSE}),
    GpcState.PAUSE: frozenset({GpcState.RUN, GpcState.CONNECT}),
    GpcState.MAINTENANCE: frozenset({GpcState.CONNECT}),
}


class InvalidStateTransition(Exception):
    """Raised when a guard or transition is rejected by the FSM."""

    def __init__(self, current: GpcState, expected: Iterable[GpcState]):
        self.current = current
        self.expected = tuple(expected)
        expected_str = ", ".join(s.value for s in self.expected) or "(none)"
        super().__init__(
            f"Operation not allowed in state '{current.value}'; "
            f"expected one of: {expected_str}."
        )


class GpcStateMachine:
    """Lock-protected state holder with transition guards and change callback."""

    def __init__(
        self,
        on_change: Callable[[GpcState], None] | None = None,
        initial: GpcState = GpcState.DISCONNECTED,
    ) -> None:
        self._lock = threading.RLock()
        self._state = initial
        self._on_change = on_change

    @property
    def state(self) -> GpcState:
        with self._lock:
            return self._state

    def require(self, *allowed: GpcState) -> None:
        """Raise InvalidStateTransition if the current state is not in *allowed*."""
        with self._lock:
            if self._state not in allowed:
                raise InvalidStateTransition(self._state, allowed)

    def transition(self, target: GpcState) -> GpcState:
        """
        Transition to *target* if the move is in the transition table.

        Re-entering the same state is a no-op and does not emit a change event.
        """
        with self._lock:
            if target == self._state:
                return self._state
            allowed = _ALLOWED.get(self._state, frozenset())
            if target not in allowed:
                raise InvalidStateTransition(self._state, allowed)
            previous = self._state
            self._state = target
            log.info("FSM: %s -> %s", previous.value, target.value)
        if self._on_change is not None:
            try:
                self._on_change(target)
            except Exception:
                log.exception("FSM: on_change callback raised")
        return target

    def force(self, target: GpcState) -> GpcState:
        """
        Set the state unconditionally (e.g. on hard error / shutdown).

        Bypasses the transition table; intended for recovery only.
        """
        with self._lock:
            if target == self._state:
                return self._state
            previous = self._state
            self._state = target
            log.warning("FSM: forced %s -> %s", previous.value, target.value)
        if self._on_change is not None:
            try:
                self._on_change(target)
            except Exception:
                log.exception("FSM: on_change callback raised")
        return target
