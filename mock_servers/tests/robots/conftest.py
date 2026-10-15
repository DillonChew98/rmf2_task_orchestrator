"""The two robot mocks, served in this process on a free port, on a fake clock.

They have no `sila` import, so unlike the SiLA mocks they can run in-process,
and a test moves their clock instead of sleeping.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from mock_servers.harness.json_http import bind
from mock_servers.mir.robot import Robot
from mock_servers.mir.server import MirApp
from mock_servers.ur.arm import Arm
from mock_servers.ur.interlock import Interlock, MirLink
from tests.fake_clock import FakeClock

DRIVE = 8.0
RUN = 8.0
FAKE_LOGIN = "Basic ZmFrZS11c2VyOmZha2UtcGFzc3dvcmQ="


@contextmanager
def serving(app):
    server = bind("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def call(method: str, url: str, body=None, headers: dict | None = None):
    """-> (status, parsed JSON or None)."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data, {"Content-Type": "application/json", **(headers or {})}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            status, raw = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    return status, json.loads(raw) if raw else None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def robot(clock) -> Robot:
    return Robot(DRIVE, clock=clock)


@pytest.fixture
def mir_url(robot):
    with serving(MirApp(robot)) as url:
        yield url


@pytest.fixture
def arm(clock) -> Arm:
    return Arm(RUN, clock=clock)


@pytest.fixture
def ur_url(arm, mir_url):
    from mock_servers.ur.server import UrApp

    with serving(UrApp(arm, Interlock(MirLink(mir_url), mir_url=mir_url))) as url:
        yield url
