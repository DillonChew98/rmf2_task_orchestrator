"""A mock started with --tls serves a cert that certs/ca.crt verifies.

Checked with a plain TLS handshake, not a SilaClient. A fifth SilaClient file would
share an xdist worker with another instrument's file and collide in the protobuf
descriptor pool (see pyproject.toml).
"""

from __future__ import annotations

import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from .conftest import free_port, wait_until_connectable

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_dev_certs.sh"


@pytest.fixture(scope="module")
def certs(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("certs")
    subprocess.run([str(SCRIPT), str(out)], check=True, capture_output=True)
    return out


def test_tls_mock_is_verified_by_the_dev_ca(certs: Path) -> None:
    port = free_port()
    cmd = [
        sys.executable, "-u", "-m", "mock_servers.x3dsc.server",
        "--port", str(port), "--no-advertise", "--tls",
        "--cert-chain", str(certs / "server.crt"),
        "--private-key", str(certs / "server.key"),
    ]
    log = Path(tempfile.gettempdir()) / f"mock-x3dsc-tls-{port}.log"
    with log.open("w") as handle:
        proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, text=True)
        try:
            wait_until_connectable(port, proc, log, 30.0)
            context = ssl.create_default_context(cafile=str(certs / "ca.crt"))
            context.set_alpn_protocols(["h2"])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname="localhost") as tls:
                    assert tls.getpeercert()["subject"][0][0] == ("commonName", "localhost")
        finally:
            proc.terminate()
            proc.wait(10)
    log.unlink(missing_ok=True)
