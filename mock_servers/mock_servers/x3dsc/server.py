"""Mock X3 DSC SiLA server.

Imports the real register_x3dsc_features and the real TriosX3dscClient, and gives
that client a fake HTTP layer. The SiLA surface is therefore the real server's
surface -- it cannot drift, because it is the same code.
"""

from __future__ import annotations

import argparse

from sila2.server import SilaServer

from ..harness.cli import run_main
from ..harness.identity import IDENTITIES
from ..harness.instrument_root import add_to_sys_path, find_instrument_root
from .instrument import X3DscInstrument, build_routes

IDENTITY = IDENTITIES["x3dsc"]

INSTRUMENT_ID = "X3DSC"


def _extra_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--instrument-id",
        default=INSTRUMENT_ID,
        help=f"TRIOS instrument id the fake REST API answers for (default {INSTRUMENT_ID})",
    )


def build(args: argparse.Namespace):
    root = find_instrument_root("x3dsc", args.instrument_root)
    add_to_sys_path(root)

    from sila.server import register_x3dsc_features  # noqa: E402  (needs sys.path first)
    from x3dsc_automation import TriosX3dscClient  # noqa: E402

    instrument = X3DscInstrument(run_seconds=args.run_seconds)
    routes = build_routes(instrument, args.instrument_id)

    # The REAL client, with only its HTTP layer replaced.
    client = TriosX3dscClient(
        "http://x3dsc.mock", args.instrument_id, urlopen_func=routes
    )

    identity = IDENTITY.at_port(args.port)
    server = SilaServer(**identity.as_server_kwargs())
    register_x3dsc_features(server, client)
    return server, identity, None


def main(argv: list[str] | None = None) -> None:
    run_main(build, IDENTITY, extra=_extra_args, argv=argv)


if __name__ == "__main__":
    main()
