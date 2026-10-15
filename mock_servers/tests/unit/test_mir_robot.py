"""The mock MiR's state, on a fake clock.

The field names and values checked here are the ones the orchestrator's
mir_move_to_waypoint template and the UR task server's arrival interlock read.
"""

from __future__ import annotations

import math

import pytest

from mock_servers.mir.robot import (
    CHARGE_MISSION,
    MOVE_MISSION,
    POSITIONS,
    MissionRejected,
    Robot,
    guid_for,
)
from tests.fake_clock import FakeClock

DRIVE = 8.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def robot(clock) -> Robot:
    return Robot(DRIVE, clock=clock)


def move(name: str) -> dict:
    return {"mission_id": MOVE_MISSION, "parameters": [{"input_name": "Position", "value": guid_for(name)}]}


def charge(name: str = "CHARGER") -> dict:
    return {
        "mission_id": CHARGE_MISSION,
        "parameters": [
            {"input_name": "chargingStationPosition", "value": guid_for(name)},
            {"input_name": "numberOfRetries", "value": 4},
        ],
    }


def where(robot: Robot) -> tuple[float, float]:
    position = robot.status()["position"]
    return position["x"], position["y"]


def is_free(status: dict) -> bool:
    """The template's own test for a MiR that can take a mission."""
    return status["state_id"] == 3 or (
        status["state_id"] == 5 and "Waiting for new mission" in status["mission_text"]
    )


# -- standing still ---------------------------------------------------------


def test_starts_parked_at_the_charger_and_free(robot):
    status = robot.status()
    assert where(robot) == POSITIONS["CHARGER"]
    assert is_free(status)
    assert status["velocity"] == {"linear": 0.0, "angular": 0.0}
    assert status["distance_to_next_target"] == 0.0


def test_every_position_resolves_by_guid_on_the_current_map(robot):
    listing = robot.positions()
    assert sorted(p["name"] for p in listing) == sorted(POSITIONS)
    for entry in listing:
        detail = robot.position(entry["guid"])
        assert detail["name"] == entry["name"]
        assert (detail["pos_x"], detail["pos_y"]) == POSITIONS[entry["name"]]
        assert detail["map_id"] == robot.status()["map_id"]


def test_an_unknown_guid_has_no_position(robot):
    assert robot.position("not-a-guid") is None


def test_positions_are_far_enough_apart_that_no_two_pass_one_arrival_check():
    names = list(POSITIONS)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert math.dist(POSITIONS[a], POSITIONS[b]) > 1.0, (a, b)


# -- one move -----------------------------------------------------------------


def test_a_move_executes_then_ends_done_at_its_target(robot, clock):
    queued = robot.post_mission(move("DSC"))
    status = robot.status()
    assert status["state_id"] == 5
    assert not is_free(status)
    assert status["velocity"]["linear"] > 0.01
    assert status["distance_to_next_target"] > 0.01
    assert robot.mission(queued["id"])["state"] == "Executing"

    clock.advance(DRIVE)
    status = robot.status()
    assert robot.mission(queued["id"])["state"] == "Done"
    assert where(robot) == POSITIONS["DSC"]
    assert is_free(status)
    assert status["velocity"] == {"linear": 0.0, "angular": 0.0}
    assert status["distance_to_next_target"] == 0.0


def test_the_position_moves_along_the_way(robot, clock):
    robot.post_mission(move("DSC"))
    clock.advance(DRIVE / 2)
    start, end = POSITIONS["CHARGER"], POSITIONS["DSC"]
    halfway = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    assert where(robot) == pytest.approx(halfway)


def test_mission_ids_count_up(robot, clock):
    first = robot.post_mission(move("DSC"))["id"]
    clock.advance(DRIVE)
    assert robot.post_mission(move("TGA"))["id"] == first + 1


def test_an_unknown_queue_id_has_no_mission(robot):
    assert robot.mission(99) is None


@pytest.mark.parametrize(
    "body",
    [
        {"mission_id": "mirconst-guid-0000-9999-actionlist00", "parameters": []},
        {"mission_id": MOVE_MISSION, "parameters": []},
        {"mission_id": MOVE_MISSION, "parameters": [{"input_name": "Position", "value": "nowhere"}]},
        {
            "mission_id": CHARGE_MISSION,
            "parameters": [{"input_name": "chargingStationPosition", "value": "nowhere"}],
        },
        {"parameters": []},
        ["not", "an", "object"],
    ],
)
def test_a_mission_it_cannot_run_is_refused_and_nothing_moves(robot, body):
    with pytest.raises(MissionRejected):
        robot.post_mission(body)
    assert is_free(robot.status())
    assert robot.missions() == []


# -- failures and the charger rescue -----------------------------------------


def test_fail_next_aborts_the_next_move_halfway_and_leaves_the_robot_away(robot, clock):
    robot.fail_next_move()
    queued = robot.post_mission(move("DSC"))
    clock.advance(DRIVE / 2)
    assert robot.mission(queued["id"])["state"] == "Aborted"
    assert is_free(robot.status())
    here = where(robot)
    assert math.dist(here, POSITIONS["DSC"]) > 0.35
    assert math.dist(here, POSITIONS["CHARGER"]) > 0.35

    clock.advance(DRIVE)
    assert where(robot) == here


def test_fail_next_spends_itself_on_one_move(robot, clock):
    robot.fail_next_move()
    robot.post_mission(move("DSC"))
    clock.advance(DRIVE)
    again = robot.post_mission(move("DSC"))
    clock.advance(DRIVE)
    assert robot.mission(again["id"])["state"] == "Done"


def test_fail_next_leaves_the_charge_mission_alone(robot, clock):
    robot.post_mission(move("TGA"))
    clock.advance(DRIVE)
    robot.fail_next_move()
    rescue = robot.post_mission(charge())
    clock.advance(DRIVE)
    assert robot.mission(rescue["id"])["state"] == "Done"
    assert where(robot) == POSITIONS["CHARGER"]
    failed = robot.post_mission(move("DSC"))
    clock.advance(DRIVE)
    assert robot.mission(failed["id"])["state"] == "Aborted"


def test_the_charge_mission_drives_to_the_station_it_names(robot, clock):
    robot.post_mission(move("TGA"))
    clock.advance(DRIVE)
    robot.post_mission(charge("CHARGER"))
    clock.advance(DRIVE)
    assert where(robot) == POSITIONS["CHARGER"]


def test_clearing_the_queue_aborts_the_running_and_the_waiting_missions(robot, clock):
    running = robot.post_mission(move("DSC"))
    waiting = robot.post_mission(move("TGA"))
    clock.advance(DRIVE / 4)
    stopped_at = where(robot)
    robot.clear_queue()
    assert robot.mission(running["id"])["state"] == "Aborted"
    assert robot.mission(waiting["id"])["state"] == "Aborted"
    assert is_free(robot.status())
    clock.advance(DRIVE * 3)
    assert where(robot) == stopped_at


# -- the queue ----------------------------------------------------------------


def test_a_second_move_waits_its_turn_then_starts_from_the_first_target(robot, clock):
    first = robot.post_mission(move("DSC"))
    second = robot.post_mission(move("TGA"))
    assert robot.mission(second["id"])["state"] == "Pending"

    clock.advance(DRIVE)
    assert robot.mission(first["id"])["state"] == "Done"
    assert robot.mission(second["id"])["state"] == "Executing"
    assert where(robot) == POSITIONS["DSC"]

    clock.advance(DRIVE)
    assert robot.mission(second["id"])["state"] == "Done"
    assert where(robot) == POSITIONS["TGA"]


def test_a_late_reader_catches_up_on_every_mission_at_once(robot, clock):
    first = robot.post_mission(move("DSC"))
    second = robot.post_mission(move("TGA"))
    clock.advance(DRIVE * 5)
    assert [robot.mission(m["id"])["state"] for m in (first, second)] == ["Done", "Done"]
    assert where(robot) == POSITIONS["TGA"]
    assert is_free(robot.status())


def test_the_status_names_the_running_mission(robot, clock):
    queued = robot.post_mission(move("DSC"))
    assert robot.status()["mission_queue_id"] == queued["id"]
    clock.advance(DRIVE)
    assert robot.status()["mission_queue_id"] is None
