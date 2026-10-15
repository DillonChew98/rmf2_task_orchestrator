"""
GPC SiLA 2 server exposing org.tainstruments/gpc/* features over gRPC.

CLI entry point:
    python -m sila.server --insecure
    gpc-sila-server --insecure
"""

from __future__ import annotations

import argparse
import logging
from argparse import ArgumentParser, Namespace

from sila2.server import SilaServer

from gpc_automation import GpcAutomation

from .tls_config import DEFAULT_CERT_CHAIN, DEFAULT_PRIVATE_KEY, load_tls_material

from .gpc_impl import (
    GpcExecutor,
    InstrumentLifecycleImpl,
    InstrumentStatusImpl,
    SequenceControlImpl,
    UiInputImpl,
    UtilityImpl,
)
from .gpc_sila.instrumentlifecycle.instrumentlifecycle_feature import (
    InstrumentLifecycleFeature,
)
from .gpc_sila.instrumentstatus.instrumentstatus_feature import InstrumentStatusFeature
from .gpc_sila.sequencecontrol.sequencecontrol_feature import SequenceControlFeature
from .gpc_sila.uiinput.uiinput_feature import UiInputFeature
from .gpc_sila.utility.utility_feature import UtilityFeature
from .state_machine import GpcState, GpcStateMachine

logger = logging.getLogger("gpc.sila")


def _build_arg_parser() -> ArgumentParser:
    p = argparse.ArgumentParser(
        description="SiLA 2 server bridging GpcAutomation (Agilent WinGPC GUI)."
    )
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=50053, help="gRPC port (default 50053)")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Use plaintext gRPC (testing only; violates SiLA transport expectations).",
    )
    p.add_argument(
        "--no-discovery",
        action="store_true",
        help="Disable mDNS / SiLA discovery broadcast.",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=30,
        help="Per-call timeout for the underlying GpcAutomation client (default 30s).",
    )
    p.add_argument(
        "--auto-connect",
        action="store_true",
        help="Eagerly call gpc.connect() at startup (state goes Disconnected -> Connect).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    p.add_argument(
        "--private-key",
        default=str(DEFAULT_PRIVATE_KEY),
        metavar="FILE",
        help=f"PEM private key for TLS (default: {DEFAULT_PRIVATE_KEY}).",
    )
    p.add_argument(
        "--cert-chain",
        default=str(DEFAULT_CERT_CHAIN),
        metavar="FILE",
        help=f"PEM certificate (chain) for TLS (default: {DEFAULT_CERT_CHAIN}).",
    )
    p.add_argument(
        "--ca-for-discovery",
        default=None,
        metavar="FILE",
        help="Optional PEM CA for mDNS discovery; defaults to --cert-chain when omitted.",
    )
    p.add_argument(
        "--auto-generate-tls",
        action="store_true",
        help="Let sila2 generate a self-signed cert instead of reading cert_tools/ (TLS mode only).",
    )
    return p


def register_features(
    server: SilaServer,
    executor: GpcExecutor,
    fsm: GpcStateMachine,
) -> InstrumentLifecycleImpl:
    """Wire all 5 feature implementations into *server*."""
    lifecycle = InstrumentLifecycleImpl(server, executor, fsm)
    server.set_feature_implementation(InstrumentLifecycleFeature, lifecycle)
    server.set_feature_implementation(
        InstrumentStatusFeature, InstrumentStatusImpl(server, executor, fsm)
    )
    server.set_feature_implementation(
        SequenceControlFeature, SequenceControlImpl(server, executor, fsm)
    )
    server.set_feature_implementation(
        UiInputFeature, UiInputImpl(server, executor, fsm)
    )
    server.set_feature_implementation(
        UtilityFeature, UtilityImpl(server, executor, fsm)
    )
    return lifecycle


def build_server(args: Namespace) -> tuple[SilaServer, GpcExecutor, GpcStateMachine]:
    gpc = GpcAutomation(timeout=args.connect_timeout)
    executor = GpcExecutor(gpc)
    fsm = GpcStateMachine()
    server = SilaServer(
        server_name="GPC SiLA Server",
        server_type="GpcSiLA",
        server_description="SiLA 2 bridge to Agilent WinGPC GUI automation.",
        server_version="0.1.0",
        server_vendor_url="https://www.agilent.com",
    )
    register_features(server, executor, fsm)
    return server, executor, fsm


def run_server_forever(
    server: SilaServer, executor: GpcExecutor, args: Namespace
) -> None:
    if args.insecure:
        server.start_insecure(
            args.host, args.port, enable_discovery=not args.no_discovery
        )
        logger.info(
            "GPC SiLA server listening (insecure) on %s:%s", args.host, args.port
        )
    else:
        if args.auto_generate_tls:
            server.start(args.host, args.port, enable_discovery=not args.no_discovery)
            logger.info(
                "GPC SiLA server listening (TLS, auto-generated cert) on %s:%s",
                args.host,
                args.port,
            )
        else:
            tls = load_tls_material(
                private_key_path=args.private_key,
                cert_chain_path=args.cert_chain,
                ca_for_discovery_path=args.ca_for_discovery or args.cert_chain,
            )
            server.start(
                args.host,
                args.port,
                private_key=tls.private_key,
                cert_chain=tls.cert_chain,
                ca_for_discovery=tls.ca_for_discovery,
                enable_discovery=not args.no_discovery,
            )
            logger.info(
                "GPC SiLA server listening (TLS, cert=%s) on %s:%s",
                args.cert_chain,
                args.host,
                args.port,
            )
    try:
        server.grpc_server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down on KeyboardInterrupt...")
    finally:
        if server.running:
            server.stop(grace_period=5.0)
        executor.shutdown()


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    server, executor, fsm = build_server(args)
    if args.auto_connect:
        try:
            executor.run(executor.gpc.connect)
            fsm.transition(GpcState.CONNECT)
            logger.info("Auto-connect: state is now %s", fsm.state.value)
        except Exception:
            logger.exception(
                "Auto-connect failed; server is up but state remains Disconnected"
            )
    run_server_forever(server, executor, args)


if __name__ == "__main__":
    main()
