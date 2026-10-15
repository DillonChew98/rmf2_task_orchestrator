"""Shared argparse surface and serve loop for the four mocks."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Callable

from sila2.server import SilaServer

from .identity import Identity

logger = logging.getLogger("mock_servers")

# The real x3dsc RunControl waits up to _RUN_START_TIMEOUT = 10s for the run to
# report started, polling every _RUN_POLL_INTERVAL = 2s
# (x3dsc/sila/x3dsc_sila/trios_impl.py:620-621). A run shorter than about 5s
# finishes inside that first phase, so the observable command returns having
# streamed almost nothing -- which defeats the point of a timed mock.
MIN_RUN_SECONDS = 5.0
DEFAULT_RUN_SECONDS = 20.0
CERTS_DIR = Path(__file__).resolve().parents[2] / "certs"

# The real autosampler poll is tighter: 0.5s interval, 5s start timeout
# (same file, lines 312-313). Simulated motion has to fit inside that window.
MOTION_SECONDS = 2.0


def build_parser(identity: Identity, extra: Callable[[argparse.ArgumentParser], None] | None = None):
    p = argparse.ArgumentParser(
        prog=f"mock-{identity.instrument}",
        description=f"Mock SiLA 2 server standing in for the {identity.instrument} instrument.",
    )
    # The real servers bind 0.0.0.0. The mocks bind loopback on purpose: sila2
    # advertises whatever the bind host resolves to, so loopback keeps the mDNS
    # record unreachable from the lab LAN while the local backend still finds it.
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=identity.port, help=f"gRPC port (default {identity.port})")
    p.add_argument(
        "--run-seconds",
        type=float,
        default=DEFAULT_RUN_SECONDS,
        help=f"how long a simulated run takes (default {DEFAULT_RUN_SECONDS:g}, minimum {MIN_RUN_SECONDS:g})",
    )
    p.add_argument(
        "--no-advertise",
        action="store_true",
        help="do not broadcast over mDNS (advertising is on by default)",
    )
    p.add_argument(
        "--tls",
        action="store_true",
        help="serve TLS with --cert-chain and --private-key instead of plaintext",
    )
    p.add_argument(
        "--cert-chain",
        default=str(CERTS_DIR / "server.crt"),
        help=f"server certificate PEM for --tls (default {CERTS_DIR / 'server.crt'})",
    )
    p.add_argument(
        "--private-key",
        default=str(CERTS_DIR / "server.key"),
        help=f"server private key PEM for --tls (default {CERTS_DIR / 'server.key'})",
    )
    p.add_argument(
        "--auto-generate-tls",
        action="store_true",
        help="serve TLS with a sila2 self-signed cert instead of plaintext",
    )
    p.add_argument(
        "--instrument-root",
        default=None,
        help="path to the instrument project, or to the marvel_digitalisation checkout",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    if extra is not None:
        extra(p)
    return p


def validate(args: argparse.Namespace) -> None:
    if args.run_seconds < MIN_RUN_SECONDS:
        raise SystemExit(
            f"--run-seconds must be at least {MIN_RUN_SECONDS:g}. The real RunControl "
            f"polls every 2s and allows 10s for the run to report started, so a shorter "
            f"run finishes before the observable command has streamed anything."
        )


def check_port_free(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0:
            raise SystemExit(
                f"port {port} is already in use. Another mock, or the older "
                f"x3_mock on 50052, may already be running."
            )


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _read_pem(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        raise SystemExit(
            f"--tls needs {path}. Run scripts/make_dev_certs.sh, or pass "
            f"--cert-chain and --private-key."
        ) from None


def serve_forever(
    server: SilaServer,
    args: argparse.Namespace,
    identity: Identity,
    on_stop: Callable[[], None] | None = None,
) -> None:
    """Start the server and block until interrupted.

    Mirrors the real servers' run_server_forever, with one deliberate addition: a
    SIGTERM handler. The real servers catch only KeyboardInterrupt, so under a
    SIGTERM from a launcher their mDNS record lingers until it times out. These are
    started and stopped by a script, so they need to leave cleanly.
    """
    enable_discovery = not args.no_advertise

    if args.tls:
        server.start(
            args.host,
            args.port,
            private_key=_read_pem(args.private_key),
            cert_chain=_read_pem(args.cert_chain),
            enable_discovery=enable_discovery,
        )
        transport = f"TLS ({args.cert_chain})"
    elif args.auto_generate_tls:
        server.start(args.host, args.port, enable_discovery=enable_discovery)
        transport = "TLS (self-signed)"
    else:
        server.start_insecure(args.host, args.port, enable_discovery=enable_discovery)
        transport = "plaintext"

    logger.info(
        "%s listening on %s:%s | %s | uuid=%s | mDNS=%s | run=%.0fs",
        identity.server_name,
        args.host,
        args.port,
        transport,
        identity.server_uuid,
        "on" if enable_discovery else "off",
        args.run_seconds,
    )

    stopping = threading.Event()

    def handle_term(signum, _frame) -> None:
        logger.info("signal %s received, stopping", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, handle_term)

    try:
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        logger.info("interrupted, stopping")
    finally:
        server.stop(grace_period=2.0)
        if on_stop is not None:
            on_stop()
        logger.info("stopped")


def run_main(build: Callable[[argparse.Namespace], tuple[SilaServer, Identity, Callable[[], None] | None]],
             identity: Identity,
             extra: Callable[[argparse.ArgumentParser], None] | None = None,
             argv: list[str] | None = None) -> None:
    """The whole boot sequence, shared by all four entry points."""
    args = build_parser(identity, extra).parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging(args.verbose)
    validate(args)
    check_port_free(args.host, args.port)
    server, resolved_identity, on_stop = build(args)
    serve_forever(server, args, resolved_identity, on_stop)
