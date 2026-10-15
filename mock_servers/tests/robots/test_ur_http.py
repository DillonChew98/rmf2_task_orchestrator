"""The mock UR task server over HTTP, its arrival interlock asking the mock MiR over HTTP.

Codes and shapes follow marvel_mm's scripts/rest_api.py.
"""

from __future__ import annotations

import pytest

from mock_servers.mir.robot import MOVE_MISSION, guid_for
from mock_servers.ur.arm import TASKS, Arm
from mock_servers.ur.interlock import Interlock, MirLink
from mock_servers.ur.server import UrApp
from tests.robots.conftest import DRIVE, RUN, call, serving

START_KEYS = {"success", "message", "task", "action", "item", "ros_task", "mir"}
HOME_KEYS = {"arm_at_home", "mir_safe_to_move", "arm_home"}


def drive_to(mir_url: str, clock, name: str) -> None:
    body = {"mission_id": MOVE_MISSION, "parameters": [{"input_name": "Position", "value": guid_for(name)}]}
    assert call("POST", mir_url + "/api/v2.0.0/mission_queue", body)[0] == 201
    clock.advance(DRIVE)


def start(ur_url: str, body=None, query: str = ""):
    return call("POST", f"{ur_url}/task/start{query}", body)


@pytest.fixture
def lone_ur(clock):
    """A UR with the interlock off, so no MiR is needed."""
    with serving(UrApp(Arm(RUN, clock=clock), Interlock(None, enabled=False))) as url:
        yield url


# -- what the templates do ------------------------------------------------------


def test_ur_perform_task_after_the_mir_reached_the_bench(ur_url, mir_url, clock):
    # mir_move_to_waypoint asks first whether the base may drive.
    status, arm = call("GET", ur_url + "/task/status")
    assert (status, arm["mir_safe_to_move"]) == (200, True)
    drive_to(mir_url, clock, "BENCH")

    # ur_perform_task: wait for ready, start, poll this task to an end state.
    assert call("GET", ur_url + "/task/status")[1]["ready"] is True
    status, started = start(ur_url, {"task": "bench", "action": "pick", "item": "DSC"})
    assert status == 200
    assert set(started) == START_KEYS
    assert (started["success"], started["ros_task"], started["item"]) == (True, "pick_task", ["DSC"])
    assert started["mir"]["distance_m"] == 0.0

    status, one = call("GET", ur_url + "/task/status?task=bench")
    assert (status, one["state"], one["ros_task"]) == (200, "EXECUTING", "pick_task")
    assert one["mir_safe_to_move"] is False
    clock.advance(RUN)
    status, one = call("GET", ur_url + "/task/status?task=bench")
    assert one["state"] == "COMPLETED"
    assert set(one) >= HOME_KEYS | {"task", "success", "message", "controller"}
    assert call("GET", ur_url + "/task/status")[1]["mir_safe_to_move"] is True


def test_the_gpc_templates_empty_item_means_the_whole_station(ur_url, mir_url, clock):
    drive_to(mir_url, clock, "GPC")
    status, started = start(ur_url, {"task": "gpc", "action": "place", "item": ""})
    assert (status, started["item"]) == (200, None)


# -- the interlock ------------------------------------------------------------------


def test_a_task_away_from_its_waypoint_is_refused_with_where_the_mir_is(ur_url):
    status, refused = start(ur_url, {"task": "dsc", "action": "place"})
    assert (status, refused["success"]) == (412, False)
    assert "from 'DSC'" in refused["message"]
    assert refused["mir"]["actual"] == [2.0, 2.0]


def test_a_task_while_the_mir_drives_is_refused(ur_url, mir_url, clock):
    body = {"mission_id": MOVE_MISSION, "parameters": [{"input_name": "Position", "value": guid_for("DSC")}]}
    call("POST", mir_url + "/api/v2.0.0/mission_queue", body)
    clock.advance(DRIVE / 2)
    status, refused = start(ur_url, {"task": "dsc"})
    assert status == 412
    assert "still moving" in refused["message"]


def test_no_mir_to_ask_is_a_503(clock):
    gone = "http://127.0.0.1:9"
    with serving(UrApp(Arm(RUN, clock=clock), Interlock(MirLink(gone, timeout=1), mir_url=gone))) as url:
        status, refused = start(url, {"task": "dsc"})
    assert status == 503
    assert gone in refused["message"]


def test_with_the_interlock_off_a_task_starts_anywhere(lone_ur):
    status, started = start(lone_ur, {"task": "dsc"})
    assert (status, started["action"], started["mir"]) == (200, "place", None)


# -- arguments --------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, code, words",
    [
        ({}, 400, "'task' is required"),
        ({"task": "nmr"}, 404, "unknown task 'nmr'"),
        ({"task": "dsc", "action": "shake"}, 400, "unknown action 'shake'"),
        ({"task": "bench", "item": "NMR"}, 400, "'bench' has no item NMR"),
    ],
)
def test_bad_arguments_are_refused_before_the_mir_is_asked(lone_ur, body, code, words):
    status, refused = start(lone_ur, body)
    assert status == code
    assert set(refused) == START_KEYS
    assert refused["success"] is False
    assert words in refused["message"]


def test_the_query_wins_over_the_body(lone_ur):
    status, started = start(lone_ur, {"task": "bench", "action": "pick"}, "?task=dsc&action=place")
    assert (status, started["task"], started["action"]) == (200, "dsc", "place")


def test_no_body_and_no_query_still_answers_in_shape(lone_ur):
    status, refused = start(lone_ur)
    assert status == 400
    assert set(refused) == START_KEYS


def test_the_bench_defaults_to_pick(lone_ur):
    assert start(lone_ur, {"task": "bench"})[1]["ros_task"] == "pick_task"


def test_a_body_that_is_not_an_object_is_a_422(lone_ur):
    assert start(lone_ur, ["dsc"])[0] == 422


# -- status ---------------------------------------------------------------------------


def test_an_idle_arm_is_ready_and_lists_every_task(lone_ur):
    status, arm = call("GET", lone_ur + "/task/status")
    assert status == 200
    assert (arm["ready"], arm["running"], arm["state"], arm["controller"]) == (True, None, "IDLE", "active")
    assert arm["tasks"] == {task: "IDLE" for task in TASKS}
    assert arm["mir_safe_to_move"] is True


def test_a_second_start_of_a_busy_task_is_a_409(lone_ur):
    start(lone_ur, {"task": "dsc"})
    status, refused = start(lone_ur, {"task": "dsc"})
    assert (status, refused["message"]) == (409, "Cannot start: EXECUTING")
    arm = call("GET", lone_ur + "/task/status")[1]
    assert (arm["ready"], arm["running"], arm["state"]) == (False, "dsc", "EXECUTING")


def test_two_busy_tasks_make_the_arm_status_a_409(lone_ur):
    start(lone_ur, {"task": "dsc"})
    start(lone_ur, {"task": "tga"})
    status, arm = call("GET", lone_ur + "/task/status")
    assert status == 409
    assert "more than one owner" in arm["message"]
    assert arm["mir_safe_to_move"] is False


@pytest.mark.parametrize("query", ["?task=nmr", "?task="])
def test_status_of_an_unknown_task_is_a_404_that_still_says_if_the_mir_may_move(lone_ur, query):
    status, one = call("GET", lone_ur + "/task/status" + query)
    assert status == 404
    assert one["success"] is False
    assert set(one) >= HOME_KEYS


def test_fail_next_ends_the_next_task_in_error(lone_ur, clock):
    assert call("POST", lone_ur + "/mock/fail-next")[0] == 200
    start(lone_ur, {"task": "gpc"})
    clock.advance(RUN)
    assert call("GET", lone_ur + "/task/status?task=gpc")[1]["state"] == "ERROR"
    arm = call("GET", lone_ur + "/task/status")[1]
    assert arm["ready"] is True
    assert "gpc ended in ERROR" in arm["message"]


def test_tasks_and_health(ur_url, mir_url):
    status, listing = call("GET", ur_url + "/tasks")
    assert status == 200
    assert listing["tasks"] == list(TASKS)
    assert listing["items"]["bench"] == ["TGA", "DSC", "GPC", "FTIR"]
    assert listing["mir_arrival_check"]["enabled"] is True
    assert call("GET", ur_url + "/health") == (200, {"status": "ok"})
    assert call("GET", ur_url + "/nowhere")[0] == 404
