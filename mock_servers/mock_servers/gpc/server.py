"""Mock GPC SiLA server.

GPC needs one thing the TRIOS mocks do not. gpc/sila/gpc_impl.py and
gpc/sila/server.py both import `gpc_automation` at module level, and that package
pulls pywinauto, pyautogui and PIL.ImageGrab -- none of which import on a headless
Linux box. So `import sila.server` dies before any mock code runs.

The fix is to register a stub `gpc_automation` in sys.modules first. That is not a
workaround so much as the same substitution one level up: the mock is already
replacing the automation object, so it replaces the module that would have
provided it. Nothing in the marvel_digitalisation repo changes.

Setting `__path__` on the stub matters. gpc_impl calls asdict() on genuine
SequenceSummary / CurrentSequenceStep / InstrumentInfo instances, so
gpc_automation.models has to stay REAL. With __path__ set, importing that
submodule resolves to the real file without ever running the package's
__init__.py -- which is the thing that reaches for pywinauto.
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

from sila2.server import SilaServer

from ..harness.cli import run_main
from ..harness.identity import IDENTITIES
from ..harness.instrument_root import add_to_sys_path, find_instrument_root
from .fake_automation import FakeGpcAutomation

IDENTITY = IDENTITIES["gpc"]


def install_automation_stub(root: Path) -> None:
    """Put a fake `gpc_automation` in place of the Windows-only real one."""
    if "gpc_automation" in sys.modules:
        return
    stub = types.ModuleType("gpc_automation")
    stub.__path__ = [str(root / "gpc_automation")]  # real submodules still import
    stub.GpcAutomation = FakeGpcAutomation
    stub.get_gpc = lambda: None
    sys.modules["gpc_automation"] = stub

    # Re-export the real dataclasses, matching what the real package's __init__ does.
    from gpc_automation.models import (  # noqa: E402
        CurrentSequenceStep,
        InstrumentInfo,
        InstrumentSnapshot,
        SequenceSummary,
    )

    stub.CurrentSequenceStep = CurrentSequenceStep
    stub.InstrumentInfo = InstrumentInfo
    stub.InstrumentSnapshot = InstrumentSnapshot
    stub.SequenceSummary = SequenceSummary


def build(args: argparse.Namespace):
    root = find_instrument_root("gpc", args.instrument_root)
    add_to_sys_path(root)
    install_automation_stub(root)

    from sila.gpc_impl import GpcExecutor  # noqa: E402
    from sila.server import register_features  # noqa: E402
    from sila.state_machine import GpcState, GpcStateMachine  # noqa: E402

    # The REAL state machine, unchanged. It is pure Python, so the mock enforces
    # the same transition table and rejects with the same gpc.state.invalid envelope as the real
    # server -- because it is the same code.
    fsm = GpcStateMachine()

    def on_run_finished() -> None:
        """Return the FSM to Connect when a sequence ends on its own.

        The FSM only moves on explicit commands, so without this CurrentState
        would sit at Run forever after a sequence completed. force() is used
        rather than transition() because it cannot raise, which is what its
        docstring reserves it for -- recovery.
        """
        fsm.force(GpcState.CONNECT)

    automation = FakeGpcAutomation(run_seconds=args.run_seconds, on_run_finished=on_run_finished)

    # The REAL executor, so the single-worker serialisation is preserved too.
    executor = GpcExecutor(automation)

    identity = IDENTITY.at_port(args.port)
    server = SilaServer(**identity.as_server_kwargs())
    register_features(server, executor, fsm)

    if args.auto_connect:
        executor.run(automation.connect)
        fsm.transition(GpcState.CONNECT)

    return server, identity, executor.shutdown


def _extra_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-connect",
        action="store_true",
        help="connect at startup, so the server begins in the Connect state",
    )


def main(argv: list[str] | None = None) -> None:
    run_main(build, IDENTITY, extra=_extra_args, argv=argv)


if __name__ == "__main__":
    main()
