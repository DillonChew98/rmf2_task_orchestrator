"""The mock MiR's state: where it is, and its mission queue.

Like TimedRun, nothing runs in the background. Every read works out from the
clock what the robot has done since the last read, so a test drives time by hand,
and a reader who comes late catches up on every mission at once.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

# Named positions on the mock map, in metres. More than 1 m apart, so the
# template's 0.3 m "already at destination" test and the UR's 0.35 m arrival
# check can never mistake one for another.
POSITIONS = {
    "CHARGER": (2.0, 2.0),
    "BENCH": (5.0, 2.0),
    "DSC": (8.0, 2.0),
    "TGA": (8.0, 5.0),
    "FTIR": (5.0, 5.0),
    "GPC": (2.0, 5.0),
    "STORAGE": (5.0, 8.0),
}
MAP_ID = "mock-map"

# The two missions the orchestrator's mir_move_to_waypoint template queues, and
# the parameter that names each one's target.
MOVE_MISSION = "mirconst-guid-0000-0001-actionlist00"
CHARGE_MISSION = "mirconst-guid-0000-0004-actionlist00"
TARGET_PARAMETER = {MOVE_MISSION: "Position", CHARGE_MISSION: "chargingStationPosition"}

# The template counts a MiR as free on state_id 3, or on 5 with "Waiting for new
# mission" in mission_text. The mock only ever reports these two.
READY, EXECUTING = 3, 5
DRIVE_SPEED_MPS = 0.5
ENDED = ("Done", "Aborted")


def guid_for(name: str) -> str:
    return f"mock-position-{name.lower()}"


_NAME_BY_GUID = {guid_for(name): name for name in POSITIONS}


class MissionRejected(ValueError):
    """A mission this mock cannot run. The server answers 400 with the reason."""


@dataclass
class _Mission:
    id: int
    mission_id: str
    parameters: list
    target: str
    posted: float
    fail: bool
    state: str = "Pending"
    origin: tuple[float, float] = (0.0, 0.0)
    started: float = 0.0
    message: str = ""

    def ends_at(self, drive: float) -> float:
        return self.started + (drive / 2 if self.fail else drive)

    def point_at(self, t: float, drive: float) -> tuple[float, float]:
        # A failed move stops halfway, away from both its start and its target.
        stop = 0.5 if self.fail else 1.0
        fraction = stop if drive <= 0 else min(stop, max(0.0, (t - self.started) / drive))
        (x0, y0), (x1, y1) = self.origin, POSITIONS[self.target]
        return x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction

    def describe(self) -> dict:
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "state": self.state,
            "parameters": self.parameters,
            "message": self.message,
            "url": f"/v2.0.0/mission_queue/{self.id}",
        }


class Robot:
    def __init__(
        self,
        drive_seconds: float = 8.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        start: str = "CHARGER",
    ) -> None:
        self.drive_seconds = float(drive_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._here = POSITIONS[start]
        self._missions: list[_Mission] = []
        self._free_at = -math.inf
        self._next_id = 1
        self._fail_next = False

    # -- reads ------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            driving = self._advance(now)
            if driving is None:
                here, remaining = self._here, 0.0
            else:
                here = driving.point_at(now, self.drive_seconds)
                remaining = math.dist(here, POSITIONS[driving.target])
            return {
                "robot_name": "Mock MiR",
                "state_id": EXECUTING if driving else READY,
                "state_text": "Executing" if driving else "Ready",
                "mode_text": "Mission",
                "mission_queue_id": driving.id if driving else None,
                "mission_text": (
                    f"Moving to '{driving.target}'..." if driving else "Waiting for new missions..."
                ),
                "position": {"x": here[0], "y": here[1], "orientation": 0.0},
                "velocity": {"linear": DRIVE_SPEED_MPS if driving else 0.0, "angular": 0.0},
                "distance_to_next_target": remaining,
                "battery_percentage": 87.5,
                "map_id": MAP_ID,
                "errors": [],
            }

    def positions(self) -> list[dict]:
        return [
            {"guid": guid_for(name), "name": name, "type_id": 0, "url": f"/v2.0.0/positions/{guid_for(name)}"}
            for name in POSITIONS
        ]

    def position(self, guid: str) -> dict | None:
        name = _NAME_BY_GUID.get(guid)
        if name is None:
            return None
        x, y = POSITIONS[name]
        return {
            "guid": guid,
            "name": name,
            "pos_x": x,
            "pos_y": y,
            "orientation": 0.0,
            "map_id": MAP_ID,
            "type_id": 0,
        }

    def missions(self) -> list[dict]:
        with self._lock:
            self._advance(self._clock())
            return [
                {"id": m.id, "state": m.state, "url": f"/v2.0.0/mission_queue/{m.id}"} for m in self._missions
            ]

    def mission(self, queue_id: int) -> dict | None:
        with self._lock:
            self._advance(self._clock())
            found = next((m for m in self._missions if m.id == queue_id), None)
            return None if found is None else found.describe()

    # -- writes -----------------------------------------------------------

    def post_mission(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise MissionRejected("the body must be a JSON object")
        mission_id = body.get("mission_id")
        wanted = TARGET_PARAMETER.get(mission_id) if isinstance(mission_id, str) else None
        if wanted is None:
            raise MissionRejected(
                f"no mission {mission_id!r} on this mock, only {MOVE_MISSION} and {CHARGE_MISSION}"
            )
        parameters = body.get("parameters") or []
        value = next(
            (p.get("value") for p in parameters if isinstance(p, dict) and p.get("input_name") == wanted),
            None,
        )
        target = _NAME_BY_GUID.get(value) if isinstance(value, str) else None
        if target is None:
            raise MissionRejected(f"{wanted} must be one of {sorted(_NAME_BY_GUID)}, got {value!r}")
        with self._lock:
            now = self._clock()
            self._advance(now)
            fail = self._fail_next and mission_id == MOVE_MISSION
            if fail:
                self._fail_next = False
            mission = _Mission(self._next_id, mission_id, parameters, target, now, fail)
            self._next_id += 1
            self._missions.append(mission)
            self._advance(now)
            return mission.describe()

    def clear_queue(self) -> None:
        with self._lock:
            now = self._clock()
            driving = self._advance(now)
            if driving is not None:
                self._here = driving.point_at(now, self.drive_seconds)
                self._free_at = now
            for mission in self._missions:
                if mission.state not in ENDED:
                    mission.state = "Aborted"
                    mission.message = "Aborted: the mission queue was cleared"

    def fail_next_move(self) -> None:
        with self._lock:
            self._fail_next = True

    # -- internals --------------------------------------------------------

    def _advance(self, now: float) -> _Mission | None:
        """Settle every mission that has ended by `now`. Returns the one driving, if any.

        A mission starts when the one before it ended, not when someone looked, so
        a late reader sees the same path as one who polled every second.
        """
        for mission in self._missions:
            if mission.state in ENDED:
                continue
            if mission.state == "Pending":
                mission.state = "Executing"
                mission.started = max(mission.posted, self._free_at)
                mission.origin = self._here
            ends = mission.ends_at(self.drive_seconds)
            if now < ends:
                return mission
            self._here = mission.point_at(ends, self.drive_seconds)
            self._free_at = ends
            if mission.fail:
                mission.state = "Aborted"
                mission.message = "Aborted by the mock, after POST /mock/fail-next"
            else:
                mission.state = "Done"
        return None
