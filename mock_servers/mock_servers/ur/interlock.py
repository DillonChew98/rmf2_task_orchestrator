"""The arrival interlock: is the MiR parked, and still, at the task's waypoint?

Same rule as rest_api.py's check_mir_arrival, and it fails closed the same way.
The mock UR asks the mock MiR over HTTP, so the two only agree when the MiR has
really driven there.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.request

from .arm import TASK_WAYPOINTS

logger = logging.getLogger("mock_servers.ur")

ARRIVAL_TOLERANCE_M = 0.35
IDLE_LINEAR_MPS = 0.01
IDLE_ANGULAR_DPS = 1.0


class MirLink:
    """Read-only HTTP calls to a MiR, the same three marvel_task's MiRClient makes."""

    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base_url}/api/v2.0.0{path}", timeout=self._timeout) as reply:
            return json.load(reply)

    def status(self) -> dict:
        return self._get("/status")

    def pose_of(self, name: str) -> tuple[float, float] | None:
        current_map = self.status().get("map_id")
        for entry in self._get("/positions"):
            if str(entry.get("name", "")).strip() != name:
                continue
            detail = self._get(f"/positions/{entry['guid']}")
            if detail.get("map_id") == current_map:
                return detail["pos_x"], detail["pos_y"]
        return None


class Interlock:
    def __init__(
        self,
        mir,
        *,
        enabled: bool = True,
        tolerance: float = ARRIVAL_TOLERANCE_M,
        mir_url: str = "",
    ) -> None:
        self.enabled = enabled
        self.tolerance = tolerance
        self._mir = mir
        self._mir_url = mir_url
        # Waypoints do not move while the mock runs.
        self._poses: dict[str, tuple[float, float]] = {}

    def info(self) -> dict:
        return {
            "enabled": self.enabled,
            "tolerance_m": self.tolerance,
            "task_waypoints": dict(TASK_WAYPOINTS),
        }

    def check(self, task: str) -> tuple[bool, int, str, dict | None]:
        """-> (ok, http_code, message, detail). `detail` goes back to the caller either way."""
        if not self.enabled:
            return True, 200, "", None
        waypoint = TASK_WAYPOINTS[task]
        try:
            target = self._poses.get(waypoint)
            if target is None:
                target = self._mir.pose_of(waypoint)
                if target is None:
                    return (
                        False,
                        412,
                        f"MiR position '{waypoint}' not found on the current map",
                        {"waypoint": waypoint},
                    )
                self._poses[waypoint] = target
            status = self._mir.status()
        except Exception as error:  # noqa: BLE001 - any failure to ask is a refusal
            logger.warning("arrival check for %r could not ask the MiR: %s", task, error)
            return (
                False,
                503,
                f"cannot reach the MiR at {self._mir_url} to confirm arrival "
                f"({type(error).__name__}). See the mock's log",
                {"waypoint": waypoint},
            )

        here = status.get("position") or {}
        if here.get("x") is None or here.get("y") is None:
            return False, 503, "MiR reported no position", {"waypoint": waypoint}
        distance = math.hypot(here["x"] - target[0], here["y"] - target[1])
        velocity = status.get("velocity") or {}
        moving = (
            abs(velocity.get("linear") or 0.0) > IDLE_LINEAR_MPS
            or abs(velocity.get("angular") or 0.0) > IDLE_ANGULAR_DPS
            or abs(status.get("distance_to_next_target") or 0.0) > IDLE_LINEAR_MPS
        )
        detail = {
            "waypoint": waypoint,
            "expected": [round(target[0], 3), round(target[1], 3)],
            "actual": [round(here["x"], 3), round(here["y"], 3)],
            "distance_m": round(distance, 3),
            "tolerance_m": self.tolerance,
            "moving": moving,
            "state": status.get("state_text"),
        }
        if moving:
            return (
                False,
                412,
                f"MiR is still moving (state {status.get('state_text')}). "
                f"Refusing to start '{task}' until it is parked at '{waypoint}'",
                detail,
            )
        if distance > self.tolerance:
            return (
                False,
                412,
                f"MiR is {distance:.2f} m from '{waypoint}'. Expected within "
                f"{self.tolerance:.2f} m before starting '{task}'",
                detail,
            )
        return True, 200, "", detail
