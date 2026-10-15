"""The mock UR task server's state and its arrival interlock, with no HTTP.

The rules mirror marvel_mm's scripts/rest_api.py and marvel_task/task_runner.py.
"""

from __future__ import annotations

import pytest

from mock_servers.ur.arm import (
    TASKS,
    Arm,
    default_action_for,
    parse_items,
)
from mock_servers.ur.interlock import Interlock
from tests.fake_clock import FakeClock

RUN = 8.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def arm(clock) -> Arm:
    return Arm(RUN, clock=clock)


# -- task states --------------------------------------------------------------


def test_every_task_starts_idle(arm):
    assert arm.states() == {task: "IDLE" for task in TASKS}
    assert not arm.busy()


def test_a_started_task_executes_then_stays_completed(arm, clock):
    assert arm.claim("dsc", "place_task") == (200, "Started")
    assert arm.states()["dsc"] == "EXECUTING"
    assert arm.busy()
    clock.advance(RUN)
    assert arm.states()["dsc"] == "COMPLETED"
    assert not arm.busy()
    clock.advance(RUN * 10)
    assert arm.states()["dsc"] == "COMPLETED"


def test_a_busy_task_refuses_a_second_start(arm):
    arm.claim("dsc", "place_task")
    assert arm.claim("dsc", "pick_task") == (409, "Cannot start: EXECUTING")
    assert arm.message("dsc").startswith("EXECUTING task:place_task")


def test_a_completed_task_starts_again(arm, clock):
    arm.claim("dsc", "place_task")
    clock.advance(RUN)
    assert arm.claim("dsc", "pick_task")[0] == 200
    assert arm.states()["dsc"] == "EXECUTING"


def test_fail_next_ends_the_next_task_in_error_once(arm, clock):
    arm.fail_next()
    arm.claim("gpc", "place_task")
    clock.advance(RUN)
    assert arm.states()["gpc"] == "ERROR"
    assert "reason: " in arm.message("gpc")
    arm.claim("gpc", "place_task")
    clock.advance(RUN)
    assert arm.states()["gpc"] == "COMPLETED"


def test_the_status_message_has_the_task_runner_format(arm, clock):
    assert arm.message("bench") == "IDLE (ctrl: active)"
    arm.claim("bench", "pick_task")
    assert arm.message("bench") == "EXECUTING task:pick_task (ctrl: active)"
    clock.advance(RUN)
    assert arm.message("bench") == "COMPLETED task:pick_task (ctrl: active)"


# -- request arguments, as rest_api.py reads them -----------------------------


@pytest.mark.parametrize(
    "raw, items",
    [
        (None, None),
        ("", None),
        ("DSC", ["DSC"]),
        ("TGA,FTIR", ["TGA", "FTIR"]),
        (["TGA", "TGA", " DSC "], ["TGA", "DSC"]),
        ("ALL", ["ALL"]),
    ],
)
def test_items_parse_as_the_real_server_parses_them(raw, items):
    assert parse_items(raw) == items


def test_the_bench_picks_by_default_and_every_machine_places():
    assert default_action_for("bench") == "pick"
    assert {default_action_for(task) for task in TASKS if task != "bench"} == {"place"}
    assert default_action_for(None) == "place"


# -- the arrival interlock ----------------------------------------------------


class FakeMir:
    def __init__(self, x=8.0, y=2.0, linear=0.0, to_target=0.0, error=None, poses=None, position=True):
        self.error = error
        self.poses = {"DSC": (8.0, 2.0)} if poses is None else poses
        self.reply = {
            "state_text": "Ready",
            "velocity": {"linear": linear, "angular": 0.0},
            "distance_to_next_target": to_target,
            "position": {"x": x, "y": y} if position else None,
        }
        self.asked = 0

    def pose_of(self, name):
        self.asked += 1
        if self.error:
            raise self.error
        return self.poses.get(name)

    def status(self):
        self.asked += 1
        if self.error:
            raise self.error
        return self.reply


def test_a_mir_parked_at_the_waypoint_passes():
    ok, code, message, detail = Interlock(FakeMir()).check("dsc")
    assert (ok, code) == (True, 200)
    assert detail["waypoint"] == "DSC"
    assert detail["distance_m"] == 0.0


def test_a_mir_half_a_metre_off_is_refused():
    ok, code, message, detail = Interlock(FakeMir(x=8.5)).check("dsc")
    assert (ok, code) == (False, 412)
    assert "0.50 m from 'DSC'" in message
    assert detail["actual"] == [8.5, 2.0]


@pytest.mark.parametrize("mir", [FakeMir(linear=0.5), FakeMir(to_target=2.0)])
def test_a_moving_mir_is_refused_even_at_the_waypoint(mir):
    ok, code, message, detail = Interlock(mir).check("dsc")
    assert (ok, code) == (False, 412)
    assert "still moving" in message
    assert detail["moving"] is True


def test_an_unknown_waypoint_is_refused():
    ok, code, message, _ = Interlock(FakeMir(poses={})).check("dsc")
    assert (ok, code) == (False, 412)
    assert "'DSC' not found on the current map" in message


def test_an_unreachable_mir_is_a_503_with_no_traceback():
    ok, code, message, _ = Interlock(
        FakeMir(error=OSError("connection refused")), mir_url="http://x:1"
    ).check("dsc")
    assert (ok, code) == (False, 503)
    assert "cannot reach the MiR at http://x:1" in message
    assert "(OSError)" in message
    assert "Traceback" not in message


def test_a_mir_with_no_position_is_a_503():
    ok, code, message, _ = Interlock(FakeMir(position=False)).check("dsc")
    assert (ok, code, message) == (False, 503, "MiR reported no position")


def test_the_waypoint_pose_is_asked_once():
    mir = FakeMir()
    interlock = Interlock(mir)
    interlock.check("dsc")
    interlock.check("dsc")
    assert mir.asked == 3


def test_a_switched_off_interlock_never_asks_the_mir():
    mir = FakeMir(x=99.0)
    assert Interlock(mir, enabled=False).check("dsc") == (True, 200, "", None)
    assert mir.asked == 0
