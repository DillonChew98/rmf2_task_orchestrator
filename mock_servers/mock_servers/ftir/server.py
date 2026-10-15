"""Mock FTIR SiLA server.

FTIR needs the same treatment as GPC, for a less obvious reason. `import
sila.server` succeeds here without Bruker's Windows-only opuspy_api wheel, because
ftir_automation imports it lazily. It is REGISTRATION that fails:
register_ftir_features builds OpusFunctionServiceImpl, whose __init__ calls
_metadata_proxy(), which imports opuspy_api at ftir/sila/ftir_impl.py:318.

So the stub goes in before register_ftir_features, not merely before the import.
Nothing in the marvel_digitalisation repo changes.
"""

from __future__ import annotations

import argparse

from sila2.server import SilaServer

from ..harness.cli import run_main
from ..harness.identity import IDENTITIES
from ..harness.instrument_root import add_to_sys_path, find_instrument_root
from . import opuspy_stub
from .fake_automation import FakeFtirAutomation

IDENTITY = IDENTITIES["ftir"]


def _extra_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-connect",
        action="store_true",
        help="open the simulated OPUS session at startup",
    )


def build(args: argparse.Namespace):
    root = find_instrument_root("ftir", args.instrument_root)
    add_to_sys_path(root)
    opuspy_stub.install()

    from sila.ftir_impl import FtirExecutor  # noqa: E402
    from sila.server import register_ftir_features  # noqa: E402

    automation = FakeFtirAutomation(run_seconds=args.run_seconds)

    # The REAL executor, so the Idle/Busy status tracking and the single-worker
    # OPUS session serialisation are preserved.
    executor = FtirExecutor(automation)

    identity = IDENTITY.at_port(args.port)
    server = SilaServer(**identity.as_server_kwargs())
    register_ftir_features(server, executor)

    if args.auto_connect:
        executor.run(automation.connect)

    # Deliberately NOT starting FtirHealthPoller. Its job is to notice a dead OPUS
    # process and set a telemetry gauge; there is no OPUS here, lab_observability
    # is not installed, and its 60s probe would only churn connect/disconnect on
    # the fake. Skipping it changes nothing on the wire.

    def on_stop() -> None:
        executor.shutdown()
        automation.close()

    return server, identity, on_stop


def main(argv: list[str] | None = None) -> None:
    run_main(build, IDENTITY, extra=_extra_args, argv=argv)


if __name__ == "__main__":
    main()
