"""The mock MiR over HTTP, in the order the orchestrator's mir_move_to_waypoint makes its calls."""

from __future__ import annotations

import http.client
import logging
from urllib.parse import urlsplit

from mock_servers.mir.robot import CHARGE_MISSION, MOVE_MISSION, POSITIONS, guid_for
from tests.robots.conftest import DRIVE, FAKE_LOGIN, call

API = "/api/v2.0.0"
LOGIN = {"Authorization": FAKE_LOGIN}


def move(name: str) -> dict:
    return {"mission_id": MOVE_MISSION, "parameters": [{"input_name": "Position", "value": guid_for(name)}]}


def test_the_status_carries_every_field_the_template_and_the_gui_read(mir_url):
    status, body = call("GET", mir_url + API + "/status", headers=LOGIN)
    assert status == 200
    wanted = {
        # mir_move_to_waypoint
        "state_id",
        "state_text",
        "mission_queue_id",
        "mission_text",
        "position",
        # the UR's arrival interlock
        "velocity",
        "distance_to_next_target",
        "map_id",
        # schedule_gui's servers.mock.json
        "robot_name",
        "mode_text",
        "battery_percentage",
        "errors",
    }
    assert wanted <= set(body)


def test_a_move_in_the_templates_call_order(mir_url, clock):
    status, target = call("GET", f"{mir_url}{API}/positions/{guid_for('DSC')}", headers=LOGIN)
    assert status == 200
    assert (target["pos_x"], target["pos_y"], target["name"], target["guid"]) == (
        8.0,
        2.0,
        "DSC",
        guid_for("DSC"),
    )

    status, before = call("GET", mir_url + API + "/status", headers=LOGIN)
    assert before["state_id"] == 3
    here = before["position"]
    assert (here["x"] - target["pos_x"]) ** 2 + (here["y"] - target["pos_y"]) ** 2 > 0.09

    status, queued = call("POST", mir_url + API + "/mission_queue", move("DSC"), headers=LOGIN)
    assert status == 201
    status, running = call("GET", f"{mir_url}{API}/mission_queue/{queued['id']}", headers=LOGIN)
    assert (status, running["state"]) == (200, "Executing")

    clock.advance(DRIVE)
    status, done = call("GET", f"{mir_url}{API}/mission_queue/{queued['id']}", headers=LOGIN)
    assert done["state"] == "Done"
    status, after = call("GET", mir_url + API + "/status")
    assert (after["position"]["x"], after["position"]["y"]) == POSITIONS["DSC"]


def test_a_failed_move_then_the_templates_rescue_to_the_charger(mir_url, clock):
    assert call("POST", mir_url + "/mock/fail-next")[0] == 200
    status, queued = call("POST", mir_url + API + "/mission_queue", move("TGA"), headers=LOGIN)
    clock.advance(DRIVE / 2)
    assert call("GET", f"{mir_url}{API}/mission_queue/{queued['id']}")[1]["state"] == "Aborted"

    assert call("DELETE", mir_url + API + "/mission_queue", headers=LOGIN) == (204, None)
    rescue = {
        "mission_id": CHARGE_MISSION,
        "parameters": [
            {"input_name": "chargingStationPosition", "value": guid_for("CHARGER")},
            {"input_name": "numberOfRetries", "value": 4},
        ],
    }
    status, charging = call("POST", mir_url + API + "/mission_queue", rescue, headers=LOGIN)
    assert status == 201
    clock.advance(DRIVE)
    assert call("GET", f"{mir_url}{API}/mission_queue/{charging['id']}")[1]["state"] == "Done"
    position = call("GET", mir_url + API + "/status")[1]["position"]
    assert (position["x"], position["y"]) == POSITIONS["CHARGER"]


def test_the_queue_lists_every_mission(mir_url):
    call("POST", mir_url + API + "/mission_queue", move("DSC"))
    status, listing = call("GET", mir_url + API + "/mission_queue")
    assert status == 200
    assert [entry["state"] for entry in listing] == ["Executing"]


def test_what_it_cannot_answer_is_a_4xx_with_a_reason(mir_url):
    bad_mission = call("POST", mir_url + API + "/mission_queue", {"mission_id": "nope", "parameters": []})
    assert bad_mission[0] == 400
    assert "nope" in bad_mission[1]["error_human"]
    assert call("GET", f"{mir_url}{API}/positions/nowhere")[0] == 404
    assert call("GET", f"{mir_url}{API}/mission_queue/77")[0] == 404
    assert call("GET", f"{mir_url}{API}/mission_queue/abc")[0] == 404
    assert call("GET", mir_url + "/elsewhere")[0] == 404
    assert call("PUT", mir_url + API + "/status")[0] == 404


def test_a_body_that_is_not_json_is_a_400(mir_url):
    parts = urlsplit(mir_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    connection.request(
        "POST", API + "/mission_queue", body=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert connection.getresponse().status == 400
    connection.close()


def test_one_connection_carries_many_requests(mir_url):
    """The orchestrator's HTTP client keeps connections open, so every answer must say where it ends."""
    parts = urlsplit(mir_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    for method, path in [
        ("GET", "/status"),
        ("DELETE", "/mission_queue"),
        ("GET", "/positions"),
        ("GET", "/status"),
    ]:
        connection.request(method, API + path, headers=LOGIN)
        reply = connection.getresponse()
        reply.read()
        assert reply.status in (200, 204)
    connection.close()


def test_the_log_names_each_call_and_never_the_login(mir_url, caplog):
    caplog.set_level(logging.DEBUG)
    call("GET", mir_url + API + "/status", headers=LOGIN)
    call("POST", mir_url + API + "/mission_queue", move("DSC"), headers=LOGIN)
    text = caplog.text
    assert "GET /api/v2.0.0/status 200" in text
    assert "POST /api/v2.0.0/mission_queue 201" in text
    assert FAKE_LOGIN.split()[1] not in text
    assert "Authorization" not in text
