"""Shared SiLA server CLI TLS flags and start/wait helpers."""

from __future__ import annotations

import logging
import signal
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .tls import load_tls_material

logger = logging.getLogger("sila_common.server")


def add_tls_args(
    parser: ArgumentParser,
    *,
    default_private_key: str | Path,
    default_cert_chain: str | Path,
) -> None:
    """Add standard TLS / discovery flags used by lab instrument SiLA servers."""
    parser.add_argument(
        "--private-key",
        default=str(default_private_key),
        metavar="FILE",
        help=f"PEM private key for TLS (default: {default_private_key}).",
    )
    parser.add_argument(
        "--cert-chain",
        default=str(default_cert_chain),
        metavar="FILE",
        help=f"PEM certificate (chain) for TLS (default: {default_cert_chain}).",
    )
    parser.add_argument(
        "--ca-for-discovery",
        default=None,
        metavar="FILE",
        help="Optional PEM CA for mDNS discovery; defaults to --cert-chain when omitted.",
    )
    parser.add_argument(
        "--auto-generate-tls",
        action="store_true",
        help="Let sila2 generate a self-signed cert instead of reading cert_tools/ (TLS mode only).",
    )


def start_sila_server(
    server: Any,
    args: Namespace,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Start *server* in insecure, auto-TLS, or file-TLS mode from *args*."""
    log = log or logger
    enable_discovery = not getattr(args, "no_discovery", False)
    if args.insecure:
        server.start_insecure(args.host, args.port, enable_discovery=enable_discovery)
        log.info("SiLA server listening (insecure) on %s:%s", args.host, args.port)
        return

    if getattr(args, "auto_generate_tls", False):
        server.start(args.host, args.port, enable_discovery=enable_discovery)
        log.info(
            "SiLA server listening (TLS, auto-generated cert) on %s:%s",
            args.host,
            args.port,
        )
        return

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
        enable_discovery=enable_discovery,
    )
    log.info(
        "SiLA server listening (TLS, cert=%s) on %s:%s",
        args.cert_chain,
        args.host,
        args.port,
    )


def install_shutdown_signals(server: Any, *, log: logging.Logger | None = None) -> None:
    """Stop gRPC on SIGINT/SIGTERM so wait_for_termination returns."""
    log = log or logger

    def _handler(signum: int, _frame: object) -> None:
        log.info("Received signal %s; stopping gRPC server...", signum)
        if server.running:
            server.stop(grace_period=5.0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)  # type: ignore[attr-defined]


def run_until_stopped(
    server: Any,
    *,
    log: logging.Logger | None = None,
    install_signals: bool = False,
    on_shutdown: Callable[[], None] | None = None,
) -> None:
    """Wait for termination, then stop the server and run optional cleanup."""
    log = log or logger
    if install_signals:
        install_shutdown_signals(server, log=log)
    try:
        server.grpc_server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        if on_shutdown is not None:
            on_shutdown()
        elif server.running:
            server.stop(grace_period=5.0)
