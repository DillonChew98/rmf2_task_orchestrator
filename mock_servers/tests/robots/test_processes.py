"""The two robot mocks as their own processes, the way run_all.sh starts them."""

from __future__ import annotations

import re
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from tests.robots.conftest import FAKE_LOGIN, call

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def launched(module: str, *args: str):
    """Start a mock on a free port and yield (url, its output so far). SIGTERM on the way out."""
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", module, "--port", "0", *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    try:
        for line in process.stdout:
            output.append(line)
            found = re.search(r"listening on [\d.]+:(\d+)", line)
            if found:
                break
        else:
            raise AssertionError("no banner:\n" + "".join(output))
        yield f"http://127.0.0.1:{found.group(1)}", output
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        output.append(rest)
        output.append(f"exit {process.returncode}\n")


def test_the_mir_answers_then_stops_on_sigterm_and_logs_no_login():
    with launched("mock_servers.mir.server", "--drive-seconds", "1") as (url, output):
        status, body = call("GET", url + "/api/v2.0.0/status", headers={"Authorization": FAKE_LOGIN})
        assert (status, body["state_text"]) == (200, "Ready")
    text = "".join(output)
    assert "mock-position-dsc" in text
    assert "GET /api/v2.0.0/status 200" in text
    assert "stopped" in text
    assert text.endswith("exit 0\n")
    assert FAKE_LOGIN.split()[1] not in text


def test_the_ur_asks_the_mir_process_before_it_starts_a_task():
    with launched("mock_servers.mir.server") as (mir_url, _):
        with launched("mock_servers.ur.server", "--mir-url", mir_url) as (ur_url, output):
            status, refused = call("POST", ur_url + "/task/start", {"task": "dsc", "action": "place"})
            assert status == 412
            assert "from 'DSC'" in refused["message"]
    assert "arrival interlock on" in "".join(output)
    assert "".join(output).endswith("exit 0\n")


def test_a_taken_port_is_named_and_the_second_mock_exits():
    with launched("mock_servers.mir.server") as (url, _):
        port = url.rsplit(":", 1)[1]
        second = subprocess.run(
            [sys.executable, "-m", "mock_servers.ur.server", "--port", port],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert second.returncode != 0
    assert f"cannot listen on 127.0.0.1:{port}" in second.stderr
    assert "already running" in second.stderr


def test_a_negative_duration_is_refused():
    refused = subprocess.run(
        [sys.executable, "-m", "mock_servers.mir.server", "--drive-seconds", "-1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert refused.returncode == 2
    assert "must be 0 or more" in refused.stderr
