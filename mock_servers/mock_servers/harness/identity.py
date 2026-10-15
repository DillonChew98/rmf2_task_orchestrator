"""Who each mock claims to be on the wire.

Two rules hold this together.

`server_type` is the REAL string the instrument reports. The backend dispatches on
it -- app/sila/constants.py keys its activity adapter and its unsafe-command guard
by server type -- so a mock that reported a different type would silently take a
different code path than the hardware it stands in for. The human tell goes in the
name and description instead, which is where a human actually reads it.

The UUID is stable and encodes the port. sila2 advertises the server UUID as the
mDNS instance name, and the backend keys its registry on it and never removes a row
on its own. A fresh uuid4 per boot would grow data/servers.json without bound.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

_MOCK_SUFFIX = (
    " -- MOCK: simulated, no hardware attached. "
    "Started from marvel/tools/mock_servers."
)


def mock_uuid(port: int) -> str:
    """A stable, obviously-fake UUID whose last digits are the port.

    Follows the convention set by backend/tests/sila/fixtures/x3_mock, whose UUID
    ends 050052 for port 50052.

    The parse check is not ceremony. backend/app/sila/domain/discovery.py drops a
    service whose mDNS instance name will not parse as a UUID, and it logs nothing
    on our side -- a silent no-show is the worst failure mode in this system.
    """
    value = f"00000000-0000-4000-8000-{port:012d}"
    uuid.UUID(value)
    return value


@dataclass(frozen=True)
class Identity:
    instrument: str
    port: int
    server_name: str
    server_type: str
    server_description: str
    # sila2 enforces SERVER_VERSION_REGEX, whose only allowed suffix separator is
    # an underscore -- "0.1.0-mock" is rejected at construction.
    server_version: str = "0.1.0_mock"
    server_vendor_url: str = "https://example.invalid/mock"

    @property
    def server_uuid(self) -> str:
        return mock_uuid(self.port)

    def at_port(self, port: int) -> "Identity":
        """Rebind to the port actually being served.

        The UUID encodes the port, so it has to follow --port. Otherwise two mocks
        of the same instrument on different ports would advertise the same UUID,
        and the backend keys its registry on exactly that.
        """
        return replace(self, port=port)

    def as_server_kwargs(self) -> dict[str, str]:
        return {
            "server_name": self.server_name,
            "server_type": self.server_type,
            "server_description": self.server_description,
            "server_version": self.server_version,
            "server_vendor_url": self.server_vendor_url,
            "server_uuid": self.server_uuid,
        }


def _identity(
    instrument: str,
    port: int,
    name: str,
    server_type: str,
    description: str,
    vendor_url: str,
) -> Identity:
    return Identity(
        instrument=instrument,
        port=port,
        server_name=f"{name} (MOCK)",
        server_type=server_type,
        server_description=description + _MOCK_SUFFIX,
        server_vendor_url=vendor_url,
    )


# Ports sit in a 5006x band. 50052 is the existing x3_mock, and the real
# instruments are on 50053 at LAN addresses -- leaving the gap keeps `ss -ltn`
# unambiguous about which is which.
IDENTITIES: dict[str, Identity] = {
    "x3dsc": _identity(
        "x3dsc",
        50061,
        "TRIOS X3 DSC SiLA Server",
        "TriosX3DSCSiLA",
        "SiLA 2 bridge to TA Instruments TRIOS REST API (X3 DSC).",
        "https://www.tainstruments.com",
    ),
    "sdt": _identity(
        "sdt",
        50062,
        "TRIOS SDT SiLA Server",
        "TriosSDTSiLA",
        "SiLA 2 bridge to TA Instruments TRIOS REST API (SDT).",
        "https://www.tainstruments.com",
    ),
    "gpc": _identity(
        "gpc",
        50063,
        "GPC SiLA Server",
        "GpcSiLA",
        "SiLA 2 bridge to Agilent WinGPC GUI automation.",
        "https://www.agilent.com",
    ),
    "ftir": _identity(
        "ftir",
        50064,
        "FTIR SiLA Server",
        "FtirSiLA",
        "SiLA 2 bridge to Bruker OPUS FTIR automation.",
        "https://www.bruker.com",
    ),
}
