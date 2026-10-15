"""
FTIR SiLA 2 server exposing com.bruker/ftir/* features over gRPC.

CLI entry point:
    python -m sila.server --insecure
    ftir-sila-server --insecure

Lab settings (OTLP, database, NAS, bind/OPUS defaults) load from ``ftir/.env``
when present; see ``.env.example``.
"""

from __future__ import annotations

import argparse
import logging
import os
from argparse import ArgumentParser, Namespace

from sila2.server import SilaServer

from sila_common.server import add_tls_args, install_shutdown_signals, run_until_stopped, start_sila_server

from ftir_automation import FtirAutomation
from ftir_automation.envfile import ftir_output_dir, load_dotenv

from .tls_config import DEFAULT_CERT_CHAIN, DEFAULT_PRIVATE_KEY

from .executor import FtirExecutor
from .ftir_impl import (
    InstrumentConnectionImpl,
    InstrumentSettingsImpl,
    InstrumentStatusImpl,
    MeasurementServiceImpl,
    OpusFunctionServiceImpl,
    StageControlServiceImpl,
)
from ftir_automation.health import FtirHealthPoller, SKIPPED, snapshot_is_up
from .ftir_sila.instrumentconnection.instrumentconnection_feature import (
    InstrumentConnectionFeature,
)
from .ftir_sila.instrumentsettings.instrumentsettings_feature import (
    InstrumentSettingsFeature,
)
from .ftir_sila.instrumentstatus.instrumentstatus_feature import (
    InstrumentStatusFeature,
)
from .ftir_sila.measurementservice.measurementservice_feature import (
    MeasurementServiceFeature,
)
from .ftir_sila.opusfunctionservice.opusfunctionservice_feature import (
    OpusFunctionServiceFeature,
)
from .ftir_sila.stagecontrolservice.stagecontrolservice_feature import (
    StageControlServiceFeature,
)

logger = logging.getLogger("ftir.sila")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_arg_parser() -> ArgumentParser:
    p = argparse.ArgumentParser(
        description="SiLA 2 server bridging FtirAutomation (Bruker OPUS HTTP API)."
    )
    p.add_argument(
        "--host",
        default=_env("FTIR_SILA_HOST", "0.0.0.0"),
        help="Bind address (default FTIR_SILA_HOST or 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=_env_int("FTIR_SILA_PORT", 50053),
        help="gRPC port (default FTIR_SILA_PORT or 50053)",
    )
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
        "--opus-ip",
        default=_env("FTIR_OPUS_IP", "127.0.0.1"),
        help="IP of the machine running OPUS with /HTTPSERVER=On "
        "(default FTIR_OPUS_IP or 127.0.0.1).",
    )
    p.add_argument(
        "--opus-port",
        type=int,
        default=_env_int("FTIR_OPUS_PORT", 100),
        help="OPUS HTTP server port (default FTIR_OPUS_PORT or 100).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    add_tls_args(p, default_private_key=DEFAULT_PRIVATE_KEY, default_cert_chain=DEFAULT_CERT_CHAIN)
    p.add_argument(
        "--otel-endpoint",
        default=_env("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
        metavar="HOST:PORT",
        help="OTLP gRPC endpoint (default: OTEL_EXPORTER_OTLP_ENDPOINT or disabled).",
    )
    p.add_argument(
        "--no-otel",
        action="store_true",
        help="Disable OpenTelemetry even if an OTLP endpoint is configured.",
    )
    p.add_argument(
        "--instrument-id",
        default=_env("LAB_INSTRUMENT_ID") or None,
        help="lab.instrument.id resource attribute (default: LAB_INSTRUMENT_ID or ftir-01).",
    )
    p.add_argument(
        "--otel-grpc-interceptor",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Patch grpc.server to extract W3C traceparent (default: on).",
    )
    return p


def _instrument_impl(impl, feature_name: str):
    try:
        from lab_observability import instrument_feature_impl
    except ImportError:
        return impl
    return instrument_feature_impl(impl, feature_name)


def register_ftir_features(
    server: SilaServer, executor: FtirExecutor
) -> InstrumentConnectionImpl:
    """Wire all 6 feature implementations into *server*."""
    connection = _instrument_impl(
        InstrumentConnectionImpl(server, executor), "InstrumentConnection"
    )
    server.set_feature_implementation(InstrumentConnectionFeature, connection)
    server.set_feature_implementation(
        InstrumentSettingsFeature,
        _instrument_impl(InstrumentSettingsImpl(server, executor), "InstrumentSettings"),
    )
    server.set_feature_implementation(
        InstrumentStatusFeature,
        _instrument_impl(InstrumentStatusImpl(server, executor), "InstrumentStatus"),
    )
    server.set_feature_implementation(
        MeasurementServiceFeature,
        _instrument_impl(MeasurementServiceImpl(server, executor), "MeasurementService"),
    )
    server.set_feature_implementation(
        StageControlServiceFeature,
        _instrument_impl(StageControlServiceImpl(server, executor), "StageControlService"),
    )
    server.set_feature_implementation(
        OpusFunctionServiceFeature,
        _instrument_impl(OpusFunctionServiceImpl(server, executor), "OpusFunctionService"),
    )
    return connection


def _maybe_patch_grpc(args: Namespace) -> None:
    if getattr(args, "no_otel", False):
        return
    enabled = getattr(args, "otel_grpc_interceptor", True)
    try:
        from lab_observability import patch_grpc_server_interceptors
    except ImportError:
        return
    patch_grpc_server_interceptors(enabled=enabled)


def build_server(args: Namespace) -> tuple[SilaServer, FtirExecutor, InstrumentConnectionImpl]:
    _maybe_patch_grpc(args)
    ftir = FtirAutomation(opus_ip=args.opus_ip, opus_port=args.opus_port)
    executor = FtirExecutor(ftir)
    server = SilaServer(
        server_name="FTIR SiLA Server",
        server_type="FtirSiLA",
        server_description="SiLA 2 bridge to Bruker OPUS FTIR automation.",
        server_version="0.1.0",
        server_vendor_url="https://www.bruker.com",
    )
    connection = register_ftir_features(server, executor)
    return server, executor, connection


def run_server_forever(server: SilaServer, executor: FtirExecutor, args: Namespace) -> None:
    start_sila_server(server, args, log=logger)
    install_shutdown_signals(server, log=logger)

    def _health_probe() -> bool | object:
        snap = executor.try_get_snapshot()
        if snap is SKIPPED:
            return SKIPPED
        return snapshot_is_up(snap)

    poller = FtirHealthPoller(
        lambda fn: fn(),
        _health_probe,
        interval_s=60.0,
    )
    poller.start()

    def _on_shutdown() -> None:
        poller.stop()
        if server.running:
            server.stop(grace_period=5.0)
        executor.shutdown()
        try:
            from lab_observability import shutdown_observability

            shutdown_observability()
        except ImportError:
            pass

    run_until_stopped(server, log=logger, on_shutdown=_on_shutdown)


def _setup_otel(args: Namespace, server_uuid: str = "") -> None:
    if getattr(args, "no_otel", False):
        return
    try:
        from lab_observability import load_config, setup_observability
    except ImportError:
        logger.info("lab-observability not installed; telemetry disabled")
        return
    cfg = load_config(
        service_name="ftir-sila-server",
        service_version="0.1.0",
        service_instance_id=server_uuid,
        instrument_id=args.instrument_id or "ftir-01",
        instrument_kind="ftir",
        instrument_vendor="bruker",
        instrument_model="INVENIO-S",
        enabled=None if args.otel_endpoint else None,
        otlp_endpoint=args.otel_endpoint,
        grpc_interceptor=args.otel_grpc_interceptor,
    )
    if args.no_otel:
        return
    setup_observability(cfg)


def main(argv: list[str] | None = None) -> None:
    loaded = load_dotenv()
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if loaded is not None:
        logger.info("Loaded environment from %s", loaded)
    output_dir = ftir_output_dir()
    if output_dir:
        logger.info("FTIR output directory: %s", output_dir)
    server, executor, _connection = build_server(args)
    _setup_otel(args, server_uuid=str(server.server_uuid))
    run_server_forever(server, executor, args)


if __name__ == "__main__":
    main()
