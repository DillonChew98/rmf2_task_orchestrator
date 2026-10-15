"""The mock UR task server's state: one task node per station, one arm.

Mirrors marvel_mm's rest_api.py and task_runner.py. A node goes IDLE ->
EXECUTING -> COMPLETED (or ERROR) and keeps that last state until its next
start. As with the mock MiR, the clock decides when a run ends, at read time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

TASKS = ("bench", "tga", "gpc", "dsc", "ftir")
TASK_ITEMS = {
    "bench": ["TGA", "DSC", "GPC", "FTIR"],
    "tga": ["TGA"],
    "gpc": ["GPC"],
    "dsc": ["DSC"],
    "ftir": ["FTIR"],
}
# The MiR position each task works from, by name, as the arrival interlock looks it up.
TASK_WAYPOINTS = {"bench": "BENCH", "tga": "TGA", "gpc": "GPC", "dsc": "DSC", "ftir": "FTIR"}
ACTIONS = ("place", "pick")
ALL_ITEMS = "ALL"
BUSY_STATES = ("EXECUTING", "PAUSED", "WAITING_FOR_CONTROLLER")


def default_action_for(task: str | None) -> str:
    return "pick" if task == "bench" else "place"


def parse_items(raw) -> list[str] | None:
    """The `item` argument as a list of names, or None when it names nothing.

    None leaves the whole station, the same as rest_api.py's _parse_items, which
    is why the GPC templates' `'item': ''` runs every GPC item.
    """
    if raw is None:
        return None
    out: list[str] = []
    for value in raw if isinstance(raw, list) else [raw]:
        for name in str(value).split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out or None


@dataclass
class _Node:
    state: str = "IDLE"
    ros_task: str = ""
    ends: float = 0.0
    fail: bool = False


class Arm:
    def __init__(self, task_seconds: float = 8.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.task_seconds = float(task_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._nodes = {task: _Node() for task in TASKS}
        self._fail_next = False

    def states(self) -> dict[str, str]:
        with self._lock:
            self._settle()
            return {task: node.state for task, node in self._nodes.items()}

    def busy(self) -> bool:
        return any(state in BUSY_STATES for state in self.states().values())

    def message(self, task: str) -> str:
        """A node's ~/status message: "<STATE>[ task:<name>] (ctrl: active)[ reason: ...]"."""
        with self._lock:
            self._settle()
            node = self._nodes[task]
            text = node.state + (f" task:{node.ros_task}" if node.ros_task else "") + " (ctrl: active)"
            if node.state == "ERROR":
                text += " reason: failed by the mock, after POST /mock/fail-next"
            return text

    def claim(self, task: str, ros_task: str) -> tuple[int, str]:
        """Start one task. -> (200, "Started"), or (409, why) when that node is busy."""
        with self._lock:
            self._settle()
            node = self._nodes[task]
            if node.state in BUSY_STATES:
                return 409, f"Cannot start: {node.state}"
            node.state = "EXECUTING"
            node.ros_task = ros_task
            node.ends = self._clock() + self.task_seconds
            node.fail, self._fail_next = self._fail_next, False
            return 200, "Started"

    def fail_next(self) -> None:
        with self._lock:
            self._fail_next = True

    def _settle(self) -> None:
        now = self._clock()
        for node in self._nodes.values():
            if node.state == "EXECUTING" and now >= node.ends:
                node.state = "ERROR" if node.fail else "COMPLETED"
