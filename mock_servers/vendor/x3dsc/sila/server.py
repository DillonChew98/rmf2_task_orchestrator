"""
TRIOS X3 DSC SiLA 2 server exposing org.tainstruments/xdsc/* features over gRPC.

CLI entry point: ``python -m sila.server`` or installed script ``x3dsc-sila-server``.
"""

from __future__ import annotations

import argparse
import logging
from argparse import ArgumentParser, Namespace

from sila2.server import SilaServer

from sila_common.server import add_tls_args, install_shutdown_signals, run_until_stopped, start_sila_server

from x3dsc_automation import TriosX3dscClient

from .tls_config import DEFAULT_CERT_CHAIN, DEFAULT_PRIVATE_KEY

from .x3dsc_sila.authentication.authentication_feature import AuthenticationFeature
from .x3dsc_sila.autosamplercontrol.autosamplercontrol_feature import AutosamplerControlFeature
from .x3dsc_sila.discovery.discovery_feature import DiscoveryFeature
from .x3dsc_sila.instrumentstatus.instrumentstatus_feature import InstrumentStatusFeature
from .x3dsc_sila.queuemanagement.queuemanagement_feature import QueueManagementFeature
from .x3dsc_sila.referencepansmanagement.referencepansmanagement_feature import ReferencePansManagementFeature
from .x3dsc_sila.runcontrol.runcontrol_feature import RunControlFeature
from .x3dsc_sila.sensorcontrol.sensorcontrol_feature import SensorControlFeature
from .x3dsc_sila.resultsexport.resultsexport_feature import ResultsExportFeature
from .x3dsc_sila.applicationcontrol.applicationcontrol_feature import ApplicationControlFeature
from .x3dsc_sila.applicationsettings.applicationsettings_feature import ApplicationSettingsFeature
from .x3dsc_sila.dialogcontrol.dialogcontrol_feature import DialogControlFeature
from .x3dsc_sila.trios_impl import (
    ApplicationControlImpl,
    ApplicationSettingsImpl,
    AuthenticationImpl,
    AutosamplerControlImpl,
    DialogControlImpl,
    DiscoveryImpl,
    InstrumentStatusImpl,
    QueueManagementImpl,
    ReferencePansManagementImpl,
    ResultsExportImpl,
    RunControlImpl,
    SensorControlImpl,
)

logger = logging.getLogger("x3dsc.sila")


def _build_arg_parser() -> ArgumentParser:
    p = argparse.ArgumentParser(description="SiLA 2 server bridging to TriosX3dscClient REST API")
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
        "--api-base-url",
        required=True,
        help="TRIOS API base, e.g. http://localhost:8080/ (see TriosX3dscClient).",
    )
    p.add_argument(
        "--instrument-id",
        required=True,
        help="Instrument id for /api/v1/instruments/{id}/...",
    )
    p.add_argument("--bearer-token", default=None, help="Optional initial Bearer token for secured routes.")
    add_tls_args(p, default_private_key=DEFAULT_PRIVATE_KEY, default_cert_chain=DEFAULT_CERT_CHAIN)
    p.add_argument(
        "--otel-endpoint",
        default=None,
        metavar="HOST:PORT",
        help="OTLP gRPC endpoint (default: OTEL_EXPORTER_OTLP_ENDPOINT or disabled).",
    )
    p.add_argument(
        "--no-otel",
        action="store_true",
        help="Disable OpenTelemetry even if an OTLP endpoint is configured.",
    )
    p.add_argument(
        "--lab-instrument-id",
        default=None,
        help="lab.instrument.id resource attribute (default: --instrument-id or LAB_INSTRUMENT_ID).",
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


def register_x3dsc_features(server: SilaServer, client: TriosX3dscClient) -> None:
    server.set_feature_implementation(
        InstrumentStatusFeature,
        _instrument_impl(InstrumentStatusImpl(server, client), "InstrumentStatus"),
    )
    server.set_feature_implementation(
        QueueManagementFeature,
        _instrument_impl(QueueManagementImpl(server, client), "QueueManagement"),
    )
    server.set_feature_implementation(
        ReferencePansManagementFeature,
        _instrument_impl(ReferencePansManagementImpl(server, client), "ReferencePansManagement"),
    )
    server.set_feature_implementation(
        AutosamplerControlFeature,
        _instrument_impl(AutosamplerControlImpl(server, client), "AutosamplerControl"),
    )
    server.set_feature_implementation(
        SensorControlFeature,
        _instrument_impl(SensorControlImpl(server, client), "SensorControl"),
    )
    server.set_feature_implementation(
        AuthenticationFeature,
        _instrument_impl(AuthenticationImpl(server, client), "Authentication"),
    )
    server.set_feature_implementation(
        DiscoveryFeature,
        _instrument_impl(DiscoveryImpl(server, client), "Discovery"),
    )
    server.set_feature_implementation(
        RunControlFeature,
        _instrument_impl(RunControlImpl(server, client), "RunControl"),
    )
    server.set_feature_implementation(
        ResultsExportFeature,
        _instrument_impl(ResultsExportImpl(server, client), "ResultsExport"),
    )
    server.set_feature_implementation(
        ApplicationControlFeature,
        _instrument_impl(ApplicationControlImpl(server, client), "ApplicationControl"),
    )
    server.set_feature_implementation(
        ApplicationSettingsFeature,
        _instrument_impl(ApplicationSettingsImpl(server, client), "ApplicationSettings"),
    )
    server.set_feature_implementation(
        DialogControlFeature,
        _instrument_impl(DialogControlImpl(server, client), "DialogControl"),
    )


def _maybe_patch_grpc(args: Namespace) -> None:
    if getattr(args, "no_otel", False):
        return
    enabled = getattr(args, "otel_grpc_interceptor", True)
    try:
        from lab_observability import patch_grpc_server_interceptors
    except ImportError:
        return
    patch_grpc_server_interceptors(enabled=enabled)


def build_server_and_client(args: Namespace) -> tuple[SilaServer, TriosX3dscClient]:
    _maybe_patch_grpc(args)
    client = TriosX3dscClient(args.api_base_url, args.instrument_id, bearer_token=args.bearer_token)
    server = SilaServer(
        server_name="TRIOS X3 DSC SiLA Server",
        server_type="TriosX3DSCSiLA",
        server_description="SiLA 2 bridge to TA Instruments TRIOS REST API (X3 DSC).",
        server_version="0.1.0",
        server_vendor_url="https://www.tainstruments.com",
    )
    register_x3dsc_features(server, client)
    return server, client


def _setup_otel(args: Namespace, server_uuid: str = "") -> None:
    if getattr(args, "no_otel", False):
        return
    try:
        from lab_observability import load_config, setup_observability
    except ImportError:
        logger.info("lab-observability not installed; telemetry disabled")
        return
    lab_id = args.lab_instrument_id or args.instrument_id or "x3dsc-01"
    cfg = load_config(
        service_name="x3dsc-sila-server",
        service_version="0.1.0",
        service_instance_id=server_uuid,
        instrument_id=lab_id,
        instrument_kind="x3dsc",
        instrument_vendor="tainstruments",
        instrument_model="X3 DSC",
        otlp_endpoint=args.otel_endpoint,
        grpc_interceptor=args.otel_grpc_interceptor,
    )
    setup_observability(cfg)


def run_server_forever(server: SilaServer, args: Namespace) -> None:
    start_sila_server(server, args, log=logger)
    install_shutdown_signals(server, log=logger)

    def _on_shutdown() -> None:
        if server.running:
            server.stop(grace_period=5.0)
        try:
            from lab_observability import shutdown_observability

            shutdown_observability()
        except ImportError:
            pass

    run_until_stopped(server, log=logger, on_shutdown=_on_shutdown)


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    server, _ = build_server_and_client(args)
    _setup_otel(args, server_uuid=str(server.server_uuid))
    run_server_forever(server, args)


if __name__ == "__main__":
    main()
