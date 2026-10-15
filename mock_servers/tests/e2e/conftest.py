"""Boots a mock in a subprocess and hands back a real SilaClient.

Subprocess, not a thread. All four instrument projects have a top-level package
named `sila`, so the first `import sila.server` in a process wins and every later
one silently reuses it. One pytest process therefore cannot host two instruments
inline. Spawning also means these tests exercise the real entry point, argparse
surface and sys.path bootstrap rather than a test-only shortcut.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from sila2.client import SilaClient
from sila_common.envelope import Envelope, parse_envelope, request

BOOT_TIMEOUT_S = 30.0
RUN_SECONDS = 6.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until_connectable(port: int, proc: subprocess.Popen, log: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mock exited early with code {proc.returncode}\n{log.read_text()}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.15)
    raise TimeoutError(f"mock did not open port {port} within {timeout}s\n{log.read_text()}")


@dataclass
class Mock:
    name: str
    port: int
    client: SilaClient
    proc: subprocess.Popen
    log: Path


def _start(name: str, run_seconds: float = RUN_SECONDS, extra: list[str] | None = None) -> Mock:
    port = free_port()
    cmd = [
        sys.executable,
        "-u",
        "-m",
        f"mock_servers.{name}.server",
        "--port",
        str(port),
        "--run-seconds",
        str(run_seconds),
        # Never advertise from a test. mDNS is host-wide and LAN-wide, and the
        # backend writes anything it hears into data/servers.json permanently.
        # backend/tests/conftest.py documents being bitten by exactly this.
        "--no-advertise",
        *(extra or []),
    ]
    # Output goes to a file, NOT subprocess.PIPE. sila2 logs an INFO line per
    # subscription event, which fills a 64KB pipe buffer in seconds; with nobody
    # draining it the server then blocks on write and the whole suite deadlocks.
    # A file also survives the process, so a failure can show what the server said.
    log = Path(tempfile.gettempdir()) / f"mock-{name}-{port}.log"
    handle = log.open("w")
    proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, text=True)
    try:
        wait_until_connectable(port, proc, log, BOOT_TIMEOUT_S)
        client = SilaClient("127.0.0.1", port, insecure=True)
    except Exception:
        proc.kill()
        handle.close()
        raise
    return Mock(name, port, client, proc, log)


def _stop(mock: Mock) -> None:
    mock.proc.terminate()
    try:
        mock.proc.wait(10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        mock.proc.kill()
    mock.log.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def x3dsc() -> Mock:
    mock = _start("x3dsc")
    yield mock
    _stop(mock)


@pytest.fixture(scope="module")
def sdt() -> Mock:
    mock = _start("sdt")
    yield mock
    _stop(mock)


@pytest.fixture(scope="module")
def gpc() -> Mock:
    mock = _start("gpc")
    yield mock
    _stop(mock)


@pytest.fixture(scope="module")
def ftir() -> Mock:
    mock = _start("ftir")
    yield mock
    _stop(mock)


def drain(command_instance) -> list:
    """Collect an observable command's intermediates until it finishes.

    Only safe to call while the command is still running. Subscribing to the
    intermediate stream of an ALREADY finished command blocks forever -- there is
    nothing left to send and no terminator arrives. Use wait_done() for anything
    you did not subscribe to immediately.
    """
    return list(command_instance.subscribe_to_intermediate_responses())


def wait_done(command_instance, timeout: float = 60.0) -> None:
    """Wait for an observable command to finish, whether or not it already has."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if command_instance.done:
            return
        time.sleep(0.1)
    raise TimeoutError(f"command {command_instance.execution_uuid} did not finish within {timeout}s")


# -- the v2 envelope ---------------------------------------------------------
# Every command takes one Request (Version, CorrelationId, TimeoutSeconds, and a
# JSON Payload) and answers one Envelope (Outcome, Code, Message, JSON Payload).
# A refusal is an Envelope with Outcome Rejected or Failed, not a SiLA error.


def send(command, payload=None, *, timeout_seconds: int = 0):
    """Call a command with a v2 Request. An observable one returns its instance."""
    return command(Request=request(payload=payload, timeout_seconds=timeout_seconds))


def ask(command, payload=None, *, timeout_seconds: int = 0) -> Envelope:
    """Call an unobservable command and return its Result envelope."""
    return parse_envelope(send(command, payload, timeout_seconds=timeout_seconds).Result)


def result_of(instance, timeout: float = 60.0) -> Envelope:
    """Wait for an observable command and return its Result envelope."""
    wait_done(instance, timeout)
    return parse_envelope(instance.get_responses().Result)


def updates_of(instance) -> list[Envelope]:
    """An observable command's Update envelopes. Same rule as drain(): call it while running."""
    return [parse_envelope(item.Update) for item in drain(instance)]
