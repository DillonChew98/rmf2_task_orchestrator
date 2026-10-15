"""A stand-in for GpcAutomation, plus the tiny widget tree the UI commands walk.

The real class drives the WinGPC desktop app through pywinauto's UIA backend, so
it cannot exist off Windows. This duck type implements every member
gpc/sila/gpc_impl.py touches -- 26 of them, extracted from that file rather than
guessed.

What is NOT faked: GpcStateMachine. That module is pure Python with no hardware in
it, so the mock reuses it unchanged and therefore enforces the exact transition
table and rejects with the exact gpc.state.invalid envelope the real server does.
"""

from __future__ import annotations

import threading
from typing import Any

from ..harness.run_engine import RunState, TimedRun

# The colour vocabulary is not invented: it is what GpcAutomation._classify_color
# and LED_COLOR_MAP actually return (gpc/gpc_automation/core.py:163-191).
COLOUR_IDLE = "green"
COLOUR_RUNNING = "yellow"
COLOUR_UNKNOWN = "gray"


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeElementInfo:
    def __init__(self, automation_id: str, control_type: str) -> None:
        self.automation_id = automation_id
        self.control_type = control_type


class FakeWidget:
    """Enough of a pywinauto wrapper for FindControl / ListChildren to describe it."""

    def __init__(
        self,
        name: str,
        auto_id: str = "",
        control_type: str = "Pane",
        children: list["FakeWidget"] | None = None,
        rect: tuple[int, int, int, int] = (0, 0, 100, 30),
    ) -> None:
        self.name = name
        self.element_info = FakeElementInfo(auto_id, control_type)
        self._children = children or []
        self._rect = FakeRect(*rect)

    def window_text(self) -> str:
        return self.name

    def rectangle(self) -> FakeRect:
        return self._rect

    def children(self) -> list["FakeWidget"]:
        return list(self._children)

    def wrapper_object(self) -> "FakeWidget":
        return self


def _default_window() -> FakeWidget:
    return FakeWidget(
        "Agilent WinGPC , Build 11494, MOCK, Instance #1",
        auto_id="MainWindow",
        control_type="Window",
        rect=(0, 0, 1280, 800),
        children=[
            FakeWidget("Instrument 1 (GPC): Sequence [DEFAULT]", "SequenceUI", "Pane", rect=(0, 60, 900, 700)),
            FakeWidget("Instrument 1 (GPC): Settings [DEFAULT]", "MethodUI", "Pane", rect=(900, 60, 1280, 700)),
            FakeWidget("Workspace", "Workspace", "Pane", rect=(0, 700, 1280, 800)),
            FakeWidget("Start", "StartButton", "Button", rect=(10, 10, 90, 40)),
        ],
    )


class ControlNotFound(RuntimeError):
    """What find_control raises when nothing matches, mirroring pywinauto's behaviour.

    FindControl catches any exception and reports exists=false, so the type only
    has to be an exception.
    """


class FakeGpcAutomation:
    """The simulated WinGPC application.

    The run lifecycle lives here: click_start() toggles the sequence on and off,
    and a running sequence finishes on its own after run_seconds, which is what
    the real instrument does when the sequence completes.
    """

    def __init__(self, run_seconds: float, on_run_finished=None) -> None:
        self._lock = threading.RLock()
        self._on_run_finished = on_run_finished
        self.run = TimedRun(run_seconds, on_settle=self._settled)
        self._start_on = False
        self._connected = False

        self.app: Any = None
        self.main_window: Any = None
        self.screenshots: list[str] = []
        self.typed: list[str] = []

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            self._connected = True
            self.app = object()
            self.main_window = _default_window()

    def bring_to_front(self) -> None:
        self._require_connected()

    def invalidate_cache(self) -> None:
        with self._lock:
            self.main_window = _default_window() if self._connected else None

    # -- the sequence run -------------------------------------------------

    def click_start(self) -> None:
        """Toggles the Start button, exactly as the real one does.

        Off -> on starts the sequence; on -> off stops it. gpc_impl reads
        get_start_button_state() around this call to drive the CONNECT <-> RUN
        transition, so the two must stay consistent.
        """
        self._require_connected()
        with self._lock:
            if self._start_on:
                self._start_on = False
                if self.run.is_running():
                    self.run.abort()
            else:
                self._start_on = True
                self.run.start(force=True)

    def get_start_button_state(self) -> bool:
        self._require_connected()
        # Reading the run settles it first, so a client polling this button sees
        # the sequence finish by itself -- which is what WinGPC does when the last
        # injection completes.
        running = self.run.is_running()
        with self._lock:
            if self._start_on and not running:
                self._start_on = False
            return self._start_on

    def _settled(self, state: RunState) -> None:
        """Fired outside TimedRun's lock when a run ends."""
        with self._lock:
            self._start_on = False
        if state is RunState.COMPLETE and self._on_run_finished is not None:
            self._on_run_finished()

    # -- status reads -----------------------------------------------------

    def get_system_status(self) -> str:
        self._require_connected()
        return COLOUR_RUNNING if self.run.is_running() else COLOUR_IDLE

    def get_analysis_status(self) -> str:
        self._require_connected()
        return COLOUR_RUNNING if self.run.is_running() else COLOUR_IDLE

    def get_color(self, auto_id: str, parent: Any = None) -> str:
        self._require_connected()
        known = {"SystemStatusLED", "AnalysisStatusLED"}
        if auto_id not in known:
            return COLOUR_UNKNOWN
        return COLOUR_RUNNING if self.run.is_running() else COLOUR_IDLE

    def get_text_in_settings_window(self, auto_id: str, parent: Any = None) -> str:
        self._require_connected()
        return f"mock value for {auto_id}"

    def get_sequence_summary(self):
        """Derived from the run, so the dashboard shows numbers that move."""
        self._require_connected()
        from gpc_automation.models import SequenceSummary

        snap = self.run.snapshot()
        total = 8
        done = int(total * snap.progress) if snap.state is RunState.RUNNING else (
            total if snap.state is RunState.COMPLETE else 0
        )
        return SequenceSummary(
            injections_elapsed=str(done),
            injections_left=str(total - done),
            time_elapsed=_mmss(snap.elapsed),
            time_left=_mmss(snap.remaining),
            solvent_elapsed=f"{done * 1.5:.1f} mL",
            solvent_left=f"{(total - done) * 1.5:.1f} mL",
        )

    def get_current_sequence_step(self):
        self._require_connected()
        from gpc_automation.models import CurrentSequenceStep

        snap = self.run.snapshot()
        if snap.state is not RunState.RUNNING:
            return CurrentSequenceStep(sequence_step="", position="", injection_time="")
        step = int(8 * snap.progress) + 1
        return CurrentSequenceStep(
            sequence_step=f"Step {step} of 8",
            position=f"Vial {step}",
            injection_time=_mmss(snap.elapsed),
        )

    def get_system_info(self):
        self._require_connected()
        from gpc_automation.models import InstrumentInfo

        colour = COLOUR_RUNNING if self.run.is_running() else COLOUR_IDLE
        return [
            InstrumentInfo(status_text="Pump: ready", color_argb=colour),
            InstrumentInfo(status_text="Detector: ready", color_argb=colour),
            InstrumentInfo(status_text="Autosampler: ready", color_argb=colour),
        ]

    def get_instrument_snapshot(self):
        self._require_connected()
        from gpc_automation.models import InstrumentSnapshot

        return InstrumentSnapshot(
            system_status=self.get_system_status(),
            analysis_status=self.get_analysis_status(),
            sequence_summary=self.get_sequence_summary(),
            current_step=self.get_current_sequence_step(),
            system_info=self.get_system_info(),
        )

    # -- UI poking, all canned -------------------------------------------

    def find_control(self, title: str | None = None, auto_id: str | None = None,
                     control_type: str | None = None) -> FakeWidget:
        self._require_connected()
        for widget in [self.main_window, *self.main_window.children()]:
            if title is not None and widget.window_text() != title:
                continue
            if auto_id is not None and widget.element_info.automation_id != auto_id:
                continue
            if control_type is not None and widget.element_info.control_type != control_type:
                continue
            return widget
        raise ControlNotFound(f"no control matching title={title!r} auto_id={auto_id!r} type={control_type!r}")

    def click_control(self, ctrl: Any) -> None:
        self._require_connected()

    def type_into_control(self, ctrl: Any, text: str, clear_first: bool = True) -> None:
        self._require_connected()
        self.typed.append(text)

    def click_menu(self, *menu_path: str) -> None:
        self._require_connected()

    def click_at(self, x: int, y: int, clicks: int = 1) -> None:
        self._require_connected()

    def type_text(self, text: str, interval: float = 0.02) -> None:
        self._require_connected()
        self.typed.append(text)

    def hotkey(self, *keys: str) -> None:
        self._require_connected()

    def wait_for_window(self, title: str, timeout: int | None = None) -> FakeWidget:
        self._require_connected()
        return FakeWidget(title, auto_id="", control_type="Window")

    def wait_until_gone(self, title: str, timeout: int = 10) -> bool:
        self._require_connected()
        return True

    def print_control_tree(self, root: Any = None, depth: int = 3) -> None:
        self._require_connected()

    def screenshot(self, filename: str = "gpc_screenshot.png") -> None:
        """Records the name and writes nothing. There is no screen to capture."""
        self._require_connected()
        self.screenshots.append(filename)

    # -- internals --------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("not connected to WinGPC")


def _mmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"
