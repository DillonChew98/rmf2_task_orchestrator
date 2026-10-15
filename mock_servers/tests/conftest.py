"""Shared fixtures.

Note what is safe to import in-process and what is not. The `<x>_automation`
packages are uniquely named, so unit tests can import the real TRIOS clients here
without trouble. The `sila` package is NOT unique -- all four instrument projects
have one -- so anything that imports `sila.server` must run in a subprocess. That
is why the end-to-end tests spawn the mocks rather than starting them inline.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Callable

import pytest

from mock_servers.harness.instrument_root import (
    add_common_to_sys_path,
    add_to_sys_path,
    find_instrument_root,
)

# The e2e tests build requests with sila_common in this process.
add_common_to_sys_path()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def x3dsc_root() -> Path:
    return find_instrument_root("x3dsc")


@pytest.fixture(scope="session")
def sdt_root() -> Path:
    return find_instrument_root("sdt")


@pytest.fixture
def x3dsc_client_factory(x3dsc_root) -> Callable:
    """Build a REAL TriosX3dscClient whose HTTP layer is a mock RouteTable."""
    add_to_sys_path(x3dsc_root)
    from x3dsc_automation import TriosX3dscClient
    from x3dsc_automation.trios_x3dsc_client import TriosX3dscClientError

    def make(table):
        client = TriosX3dscClient(
            "http://trios.invalid", table.instrument_id, urlopen_func=table
        )
        return client, TriosX3dscClientError

    return make


@pytest.fixture
def sdt_client_factory(sdt_root) -> Callable:
    add_to_sys_path(sdt_root)
    from sdt_automation import TriosSdtClient
    from sdt_automation.trios_sdt_client import TriosSdtClientError

    def make(table):
        client = TriosSdtClient("http://trios.invalid", table.instrument_id, urlopen_func=table)
        return client, TriosSdtClientError

    return make
