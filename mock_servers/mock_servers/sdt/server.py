"""Mock SDT SiLA server.

Same construction as the X3 DSC mock: the real register_sdt_features and the real
TriosSdtClient, with only the client's HTTP layer replaced.
"""

from __future__ import annotations

import argparse

from sila2.server import SilaServer

from ..harness.cli import run_main
from ..harness.identity import IDENTITIES
from ..harness.instrument_root import add_to_sys_path, find_instrument_root
from .instrument import SdtInstrument, build_routes

IDENTITY = IDENTITIES["sdt"]

INSTRUMENT_ID = "SDT"


def _extra_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--instrument-id",
        default=INSTRUMENT_ID,
        help=f"TRIOS instrument id the fake REST API answers for (default {INSTRUMENT_ID})",
    )


def build(args: argparse.Namespace):
    root = find_instrument_root("sdt", args.instrument_root)
    add_to_sys_path(root)

    from sdt_automation import TriosSdtClient  # noqa: E402  (needs sys.path first)
    from sila.server import register_sdt_features  # noqa: E402

    instrument = SdtInstrument(run_seconds=args.run_seconds)
    routes = build_routes(instrument, args.instrument_id)

    client = TriosSdtClient("http://sdt.mock", args.instrument_id, urlopen_func=routes)

    identity = IDENTITY.at_port(args.port)
    server = SilaServer(**identity.as_server_kwargs())
    register_sdt_features(server, client)
    return server, identity, None


def main(argv: list[str] | None = None) -> None:
    run_main(build, IDENTITY, extra=_extra_args, argv=argv)


if __name__ == "__main__":
    main()
