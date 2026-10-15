"""RouteTable dispatch and error mapping.

The error tests matter most: they prove an ApiError raised in a handler comes back
out of the REAL TRIOS client as a TriosX3dscClientError carrying the message and
the canForce flag. That path is the reason for faking urlopen rather than the
whole client, so it needs a test of its own.
"""

from __future__ import annotations

import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest

import pytest

from mock_servers.harness.http_fake import ApiError, RouteTable, ok


@pytest.fixture
def table() -> RouteTable:
    return RouteTable("X3DSC")


def call(table: RouteTable, method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = UrlRequest(url=url, data=data, method=method)
    with table(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


BASE = "http://trios.invalid/api/v1/instruments/X3DSC"
COMMON = "http://trios.invalid/api"


def test_instrument_route_dispatches(table):
    @table.instrument("GET", "/status")
    def _status(req):
        return ok({"procedureState": "Idle"})

    assert call(table, "GET", f"{BASE}/status") == {
        "success": True,
        "data": {"procedureState": "Idle"},
    }


def test_common_and_instrument_routes_do_not_collide(table):
    @table.instrument("GET", "/status")
    def _instrument(req):
        return ok("instrument")

    @table.common("GET", "/status")
    def _common(req):
        return ok("common")

    assert call(table, "GET", f"{BASE}/status")["data"] == "instrument"
    assert call(table, "GET", f"{COMMON}/status")["data"] == "common"


def test_path_placeholder_becomes_a_param(table):
    @table.instrument("GET", "/queue/experiments/{experimentId}")
    def _one(req):
        return ok(req.params["experimentId"])

    assert call(table, "GET", f"{BASE}/queue/experiments/abc-123")["data"] == "abc-123"


def test_query_parameters_are_parsed(table):
    @table.instrument("POST", "/status/wait-idle")
    def _wait(req):
        return ok({"timeout": req.q_int("timeout"), "force": req.q_bool("force")})

    got = call(table, "POST", f"{BASE}/status/wait-idle?timeout=45&force=true")["data"]
    assert got == {"timeout": 45, "force": True}


def test_json_body_reaches_the_handler(table):
    @table.instrument("POST", "/queue/experiments/new")
    def _add(req):
        return ok(req.json_body()["name"])

    assert call(table, "POST", f"{BASE}/queue/experiments/new", {"name": "run-1"})["data"] == "run-1"


def test_method_is_part_of_the_key(table):
    @table.instrument("GET", "/sensors")
    def _get(req):
        return ok("read")

    with pytest.raises(HTTPError) as exc:
        call(table, "PUT", f"{BASE}/sensors")
    assert exc.value.code == 404


def test_unregistered_route_is_a_404(table):
    with pytest.raises(HTTPError) as exc:
        call(table, "GET", f"{BASE}/nope")
    assert exc.value.code == 404


def test_calls_are_recorded(table):
    @table.instrument("GET", "/status")
    def _status(req):
        return ok()

    call(table, "GET", f"{BASE}/status")
    call(table, "GET", f"{BASE}/status")
    assert table.call_count("GET", "/status") == 2


# -- the part that matters: errors survive the real client ----------------


def test_api_error_becomes_an_http_error_with_the_message(table):
    @table.instrument("GET", "/status")
    def _boom(req):
        raise ApiError(409, "a run is already in progress", can_force=True)

    with pytest.raises(HTTPError) as exc:
        call(table, "GET", f"{BASE}/status")

    assert exc.value.code == 409
    payload = json.loads(exc.value.read())
    assert payload["error"]["message"] == "a run is already in progress"
    assert payload["error"]["canForce"] is True


def test_error_maps_through_the_real_trios_client(table, x3dsc_client_factory):
    """End to end through the actual TriosX3dscClient error path.

    This is what a whole-client fake would skip, and it is the exact string the
    dashboard renders when a run is refused.
    """
    @table.common("POST", "/control/start")
    def _start(req):
        raise ApiError(409, "a run is already in progress", can_force=True)

    client, error_cls = x3dsc_client_factory(table)

    with pytest.raises(error_cls) as exc:
        client.control_start()

    assert exc.value.status_code == 409
    assert exc.value.message == "a run is already in progress"
    assert exc.value.can_force is True


def test_success_maps_through_the_real_trios_client(table, x3dsc_client_factory):
    @table.instrument("GET", "/status")
    def _status(req):
        return ok({"procedureState": "Test", "instrumentIsBusy": True})

    client, _ = x3dsc_client_factory(table)
    got = client.get_instrument_status()
    assert got == {"success": True, "data": {"procedureState": "Test", "instrumentIsBusy": True}}


def test_a_handler_waiting_in_released_lets_other_requests_through(table):
    """A TRIOS wait blocks for as long as a run. Other calls must not queue behind it."""
    waiting, finish = threading.Event(), threading.Event()

    @table.instrument("POST", "/status/wait-idle")
    def _wait(req):
        with table.released():
            waiting.set()
            finish.wait(5)
        return ok("waited")

    @table.instrument("GET", "/status")
    def _status(req):
        return ok("idle")

    waiter = threading.Thread(target=call, args=(table, "POST", f"{BASE}/status/wait-idle"))
    waiter.start()
    try:
        assert waiting.wait(5)
        t0 = time.monotonic()
        assert call(table, "GET", f"{BASE}/status")["data"] == "idle"
        assert time.monotonic() - t0 < 1.0
    finally:
        finish.set()
        waiter.join(5)
