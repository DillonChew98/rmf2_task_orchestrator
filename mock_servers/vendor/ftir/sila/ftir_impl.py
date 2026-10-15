"""
SiLA feature implementations bridging FtirAutomation (Bruker OPUS).

All OPUS calls are serialized through a single-threaded executor so the
opuspy_api HTTP socket is only used from one thread, regardless of how many
SiLA gRPC worker threads dispatch commands concurrently.

Instrument-touching commands reuse one persistent OPUS HTTP session
(connect once, disconnect on Disconnect or server shutdown). Metadata-only
commands do not need a session.

FeatureVersion 2.0 for most features (MeasurementService is 3.0): every command
takes Request and returns Envelope (``sila_common.envelope``). Describable OPUS
failures stay in the envelope; ``InternalError`` is reserved for cases where an
envelope cannot be built.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from queue import Empty, Full, Queue
from typing import Any, Callable, Mapping, Optional, TypeVar

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses, SilaServer

from sila_common.envelope import (
    AnyPayload,
    Code,
    failed,
    rejected,
    running,
    succeeded,
    try_parse_request,
)

from ftir_automation import MeasurementConfig, SpectrumData
from ftir_automation.health import SKIPPED
from ftir_automation.models.instrument import InstrumentSnapshot as OpusInstrumentSnapshot
from ftir_automation.opus_bindings import OpusFunctionsProxy
from ftir_automation.telemetry import get_telemetry, sc

from . import payloads
from .codec import dumps_json, loads_mapping
from .executor import (
    NOT_CONNECTED_MSG,
    STATUS_BUSY,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_UNREACHABLE,
    FtirExecutor,
)

from .ftir_sila.instrumentconnection.instrumentconnection_base import (
    InstrumentConnectionBase,
)
from .ftir_sila.instrumentconnection.instrumentconnection_types import (
    Connect_Responses,
    Disconnect_Responses,
    HideOpus_Responses,
    ShowOpus_Responses,
)

from .ftir_sila.instrumentstatus.instrumentstatus_base import InstrumentStatusBase

from .ftir_sila.instrumentsettings.instrumentsettings_base import InstrumentSettingsBase

from .ftir_sila.measurementservice.measurementservice_base import MeasurementServiceBase
from .ftir_sila.measurementservice.measurementservice_types import (
    ExportAsm_Responses,
    MeasureBackground_IntermediateResponses,
    MeasureBackground_Responses,
    MeasureSample_IntermediateResponses,
    MeasureSample_Responses,
    ReadParameters_Responses,
    ReadSpectrum_Responses,
)

from .ftir_sila.stagecontrolservice.stagecontrolservice_base import StageControlServiceBase
from .ftir_sila.stagecontrolservice.stagecontrolservice_types import (
    CloseDrawer_IntermediateResponses,
    CloseDrawer_Responses,
    GetNumberOfAxes_Responses,
    GetStageName_Responses,
    GetStagePosition_Responses,
    IsReferencePlateAvailable_Responses,
    LoadPlate_Responses,
    MoveStage_Responses,
    MoveStageToReferencePosition_Responses,
    MoveToWell_Responses,
    OpenDrawer_IntermediateResponses,
    OpenDrawer_Responses,
    SetReferencePosition_Responses,
    UnloadPlate_Responses,
)

from .ftir_sila.opusfunctionservice.opusfunctionservice_base import OpusFunctionServiceBase
from .ftir_sila.opusfunctionservice.opusfunctionservice_types import (
    DescribeFunction_Responses,
    ExecuteFunction_Responses,
    ListFunctions_Responses,
    RawCommand_Responses,
)


class _UnknownOpusFunction(Exception):
    """Raised inside the OPUS worker when a function name is not in the catalogue."""

log = logging.getLogger(__name__)

T = TypeVar("T")

STATE_DISCONNECTED = "Disconnected"

_STATUS_PROPERTY_POLL_INTERVAL_SECONDS = 0.5


def _payload_mapping(payload: Any) -> dict[str, Any]:
    """Normalize Request.Payload to a PascalCase field dict.

    Accepts None (empty), JSON objects, AnyPayload instances, and
    sila2 Structure NamedTuples.
    """
    if payload is None:
        return {}
    if isinstance(payload, AnyPayload):
        return payload.as_dict()
    if isinstance(payload, Mapping):
        return dict(payload)
    if hasattr(payload, "_asdict"):
        return dict(payload._asdict())
    raise ValueError(f"Request.Payload must be a JSON object or empty; got {type(payload).__name__}")


def _typed_payload(cls: type[T], payload: Any) -> T:
    """Build a typed payload dataclass from decoded Request.Payload JSON."""
    return cls.from_mapping(_payload_mapping(payload))  # type: ignore[return-value]


def _require_request(
    raw: Any,
    response_cls: type[T],
) -> tuple[Any, T | None]:
    """Parse Request; on invalid JSON return ``(None, response_cls(Result=rejected))``."""
    req, err = try_parse_request(raw)
    if err is not None:
        return None, response_cls(Result=err)  # type: ignore[call-arg]
    return req, None


def _payload_get(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    return bool(value)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_object(value: Any) -> dict[str, Any]:
    if value is None or value == "" or value == {}:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return loads_mapping(value)
    raise ValueError(f"Expected object; got {type(value).__name__}")


def _empty_object(value: Any) -> dict[str, Any]:
    """Treat empty / null as ``{}`` for open-map JSON object fields."""
    if value is None or value == "" or value == {}:
        return {}
    return _as_object(value)


def _set_lifetime(
    instance: ObservableCommandInstanceWithIntermediateResponses[Any],
    timeout_seconds: int | None,
) -> None:
    if timeout_seconds is not None and timeout_seconds > 0:
        instance.lifetime_of_execution = timedelta(seconds=timeout_seconds)


def _session_result(
    exec_: FtirExecutor,
    fn: Callable[..., T],
    *args: Any,
    label: str,
    correlation_id: str = "",
    reraise: tuple[type[BaseException], ...] = (),
    **kwargs: Any,
) -> tuple[T | None, dict[str, Any] | None]:
    """Run *fn* on the persistent OPUS session.

    Returns ``(result, None)`` on success or ``(None, envelope)`` on a
    describable failure. Exceptions in *reraise* propagate unchanged.
    """
    try:
        return exec_.run_with_session(fn, *args, **kwargs), None
    except reraise:
        raise
    except (ConnectionError, ImportError) as e:
        log.exception("%s not connected", label)
        return None, failed(
            code=Code.OPUS_NOT_CONNECTED,
            message=str(e) or NOT_CONNECTED_MSG,
            correlation_id=correlation_id,
        )
    except Exception as e:
        log.exception("%s failed", label)
        return None, failed(
            code=Code.OPUS_API,
            message=str(e),
            correlation_id=correlation_id,
        )


def _metadata_proxy() -> OpusFunctionsProxy:
    """Build an OpusFunctionsProxy that serves metadata without an OPUS connection."""
    from opuspy_api.opus_commands.generated.opus_functions import (  # type: ignore[import]
        OpusFunctions,
    )

    class _MetaApi:
        opus_functions = OpusFunctions.__new__(OpusFunctions)
        parameters = None

    return OpusFunctionsProxy(_MetaApi())


def _spectra_blocks(spectra: list[SpectrumData], include_data: bool) -> list[payloads.SpectrumBlock]:
    """Build SpectrumBlock AnyPayload list for Envelope Payload."""
    blocks: list[payloads.SpectrumBlock] = []
    for s in spectra:
        lo, hi = s.wavenumber_range
        wavenumbers: list[float] = []
        intensities: list[payloads.IntensityTrace] = []
        if include_data:
            wavenumbers = [float(v) for v in s.wavenumbers.tolist()]
            arr = s.intensities
            if arr.ndim == 1:
                intensities = [payloads.IntensityTrace(Values=[float(v) for v in arr.tolist()])]
            elif arr.size:
                intensities = [
                    payloads.IntensityTrace(Values=[float(v) for v in arr[:, col].tolist()])
                    for col in range(arr.shape[1])
                ]
        blocks.append(
            payloads.SpectrumBlock(
                BlockName=s.block_name or "",
                NPoints=int(s.n_points),
                NTraces=int(s.n_traces),
                WavenumberMin=float(lo),
                WavenumberMax=float(hi),
                FilePath=s.file_path or "",
                Wavenumbers=wavenumbers,
                Intensities=intensities,
            )
        )
    return blocks


def _measure_sample_payload(
    *,
    file_path: str = "",
    asm_path: str = "",
    spectra: list[SpectrumData] | None = None,
    raw_response: list | None = None,
    include_data: bool = False,
) -> payloads.MeasureSampleResult:
    return payloads.MeasureSampleResult(
        FilePath=file_path or "",
        AsmPath=asm_path or "",
        Spectra=_spectra_blocks(spectra or [], include_data),
        RawResponse=[str(r) for r in (raw_response or [])],
    )


def _drawer_payload(function: str, response: list | None) -> payloads.DrawerResult:
    return payloads.DrawerResult(
        Function=function,
        Response=[str(r) for r in (response or [])],
    )


def _snapshot_payload(snapshot: OpusInstrumentSnapshot) -> payloads.InstrumentSnapshot:
    return payloads.InstrumentSnapshot(
        Connected=bool(snapshot.connected),
        OpusVersion=snapshot.opus_version or "",
        OpusVersionExt=snapshot.opus_version_ext or "",
        HostIp=snapshot.host_ip or "",
        Language=snapshot.language or "",
    )


def _emit_measure_update(
    instance: ObservableCommandInstanceWithIntermediateResponses[Any],
    intermediate_cls: type,
    percent: int,
    status: str,
    *,
    correlation_id: str = "",
) -> None:
    pct = max(0, min(100, int(percent)))
    instance.progress = pct / 100.0
    instance.send_intermediate_response(
        intermediate_cls(
            Update=running(
                payload=payloads.MeasureProgress(Percent=pct, Status=status),
                message=status,
                correlation_id=correlation_id,
            )
        )
    )


# ---------------------------------------------------------------------------
# InstrumentStatus
# ---------------------------------------------------------------------------


class InstrumentStatusImpl(InstrumentStatusBase):
    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self.update_CurrentInstrumentStatus(STATUS_IDLE)
        executor.set_status_listener(self._on_executor_status)

    def _on_executor_status(self, status: str) -> None:
        self.update_CurrentInstrumentStatus(status)


# ---------------------------------------------------------------------------
# InstrumentSettings
# ---------------------------------------------------------------------------


def _settings_from_snapshot(snapshot: OpusInstrumentSnapshot) -> dict[str, Any]:
    return {
        "Connected": bool(snapshot.connected),
        "OpusVersion": snapshot.opus_version or "",
        "OpusVersionExt": snapshot.opus_version_ext or "",
        "HostIp": snapshot.host_ip or "",
        "Language": snapshot.language or "",
    }


def _put_latest(queue: Queue[Any], value: Any) -> None:
    while True:
        try:
            queue.get_nowait()
        except Empty:
            break
    try:
        queue.put_nowait(value)
    except Full:
        pass


def _start_ftir_settings_poller(impl: "InstrumentSettingsImpl") -> None:
    def _run() -> None:
        while True:
            try:
                impl._fanout_settings(impl._fetch_settings())
            except Exception:
                log.exception("instrument settings poller tick failed")
            time.sleep(_STATUS_PROPERTY_POLL_INTERVAL_SECONDS)

    threading.Thread(target=_run, daemon=True, name="ftir-settings-poller").start()


class InstrumentSettingsImpl(InstrumentSettingsBase):
    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._cached_snapshot = OpusInstrumentSnapshot()
        self._sub_lock = threading.Lock()
        self._subscription_queues: list[Queue[Any]] = []
        self._poller_started = False
        # Keep listening so Busy/Idle transitions still refresh the snapshot fanout.
        executor.set_status_listener(self._on_executor_status)

    def _fetch_settings(self) -> dict[str, Any]:
        try:
            result = self._exec.try_get_snapshot()
            if result is SKIPPED:
                snapshot = self._cached_snapshot
            else:
                snapshot = result
                self._cached_snapshot = snapshot
        except Exception:
            log.debug("instrument settings snapshot read failed", exc_info=True)
            snapshot = OpusInstrumentSnapshot(connected=False)
        return _settings_from_snapshot(snapshot)

    def _fanout_settings(self, settings: dict[str, Any]) -> None:
        with self._sub_lock:
            queues = list(self._subscription_queues)
        for queue in queues:
            _put_latest(queue, settings)

    def _on_executor_status(self, activity: str) -> None:
        del activity
        self._fanout_settings(_settings_from_snapshot(self._cached_snapshot))

    @property
    def current_CurrentInstrumentSettings(self) -> dict[str, Any]:
        return self._fetch_settings()

    def CurrentInstrumentSettings_on_subscription(
        self, *, metadata: MetadataDict
    ) -> Optional[Queue[Any]]:
        queue: Queue[Any] = Queue(maxsize=1)
        with self._sub_lock:
            self._subscription_queues.append(queue)
            start_poller = not self._poller_started
            if start_poller:
                self._poller_started = True
        if start_poller:
            _start_ftir_settings_poller(self)
        _put_latest(queue, self._fetch_settings())
        return queue


# ---------------------------------------------------------------------------
# InstrumentConnection
# ---------------------------------------------------------------------------


class InstrumentConnectionImpl(InstrumentConnectionBase):
    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self._exec = executor

    def Connect(self, Request: Any, *, metadata: MetadataDict) -> Connect_Responses:
        req, _bad_req = _require_request(Request, Connect_Responses)
        if _bad_req is not None:
            return _bad_req
        result, err = _session_result(
            self._exec,
            self._exec.ftir.get_snapshot,
            label="Connect",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return Connect_Responses(Result=err)
        assert result is not None
        return Connect_Responses(
            Result=succeeded(
                payload=_snapshot_payload(result),
                correlation_id=req.correlation_id,
            )
        )

    def Disconnect(self, Request: Any, *, metadata: MetadataDict) -> Disconnect_Responses:
        req, _bad_req = _require_request(Request, Disconnect_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            self._exec.run(self._exec.ftir.disconnect)
        except Exception:
            log.exception("Disconnect failed; reporting Disconnected anyway")
        return Disconnect_Responses(
            Result=succeeded(
                payload=payloads.DisconnectResult(State=STATE_DISCONNECTED),
                correlation_id=req.correlation_id,
            )
        )

    def ShowOpus(self, Request: Any, *, metadata: MetadataDict) -> ShowOpus_Responses:
        req, _bad_req = _require_request(Request, ShowOpus_Responses)
        if _bad_req is not None:
            return _bad_req
        result, err = _session_result(
            self._exec,
            self._exec.ftir.show_opus,
            label="ShowOpus",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return ShowOpus_Responses(Result=err)
        return ShowOpus_Responses(
            Result=succeeded(
                payload=payloads.ShowOpusResult(Shown=bool(result)),
                correlation_id=req.correlation_id,
            )
        )

    def HideOpus(self, Request: Any, *, metadata: MetadataDict) -> HideOpus_Responses:
        req, _bad_req = _require_request(Request, HideOpus_Responses)
        if _bad_req is not None:
            return _bad_req
        result, err = _session_result(
            self._exec,
            self._exec.ftir.hide_opus,
            label="HideOpus",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return HideOpus_Responses(Result=err)
        return HideOpus_Responses(
            Result=succeeded(
                payload=payloads.HideOpusResult(Hidden=bool(result)),
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# MeasurementService
# ---------------------------------------------------------------------------


class MeasurementServiceImpl(MeasurementServiceBase):
    _ASM_CONTEXT_FIELDS = {
        "detector_type": "detector type",
        "beamsplitter_type": "beamsplitter type",
        "equipment_serial": "equipment serial number",
        "last_sample_id": "sample identifier",
        "last_measurement_id": "measurement identifier",
        "last_measurement_time": "measurement time",
        "last_analyst": "analyst",
    }

    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._lock = threading.Lock()
        self._last: dict[str, str] = dict.fromkeys(self._ASM_CONTEXT_FIELDS, "")
        self._last["asm_missing_json"] = "[]"

    def _get_last(self, key: str) -> str:
        with self._lock:
            return self._last[key]

    def get_DetectorType(self, *, metadata: MetadataDict) -> str:
        return self._get_last("detector_type")

    def get_BeamsplitterType(self, *, metadata: MetadataDict) -> str:
        return self._get_last("beamsplitter_type")

    def get_EquipmentSerialNumber(self, *, metadata: MetadataDict) -> str:
        return self._get_last("equipment_serial")

    def get_LastSampleIdentifier(self, *, metadata: MetadataDict) -> str:
        return self._get_last("last_sample_id")

    def get_LastMeasurementIdentifier(self, *, metadata: MetadataDict) -> str:
        return self._get_last("last_measurement_id")

    def get_LastMeasurementTime(self, *, metadata: MetadataDict) -> str:
        return self._get_last("last_measurement_time")

    def get_LastAnalyst(self, *, metadata: MetadataDict) -> str:
        return self._get_last("last_analyst")

    def get_LastAsmMissingRequiredJson(self, *, metadata: MetadataDict) -> str:
        return self._get_last("asm_missing_json")

    def _record_asm_context(self, asm_path: str) -> None:
        if not asm_path:
            return
        try:
            import json
            from pathlib import Path

            from ftir_automation.asm import validate_ftir_asm

            doc = json.loads(Path(asm_path).read_text(encoding="utf-8"))
            missing = validate_ftir_asm(doc)
            with self._lock:
                for key, field in self._ASM_CONTEXT_FIELDS.items():
                    self._last[key] = str(doc.get(field) or "")
                self._last["asm_missing_json"] = dumps_json(missing)
        except Exception:
            log.exception("Failed to record ASM context from %s", asm_path)

    def _build_config_from_payload(
        self,
        payload: dict[str, Any],
        *,
        sample_scans: int | None = None,
        gain_opus_key: str = "SGN",
        sample_name: str = "",
        reference_snm: bool = False,
    ) -> MeasurementConfig:
        config = MeasurementConfig()
        resolution = _as_float(_payload_get(payload, "Resolution"), 0.0)
        background_scans = _as_int(_payload_get(payload, "BackgroundScans"), 0)
        if sample_scans is None:
            sample_scans = _as_int(_payload_get(payload, "SampleScans"), 0)
        output_path = _as_str(_payload_get(payload, "OutputPath"), "")
        experiment_file = _as_str(_payload_get(payload, "ExperimentFile"), "")
        optical_velocity = _as_float(_payload_get(payload, "OpticalVelocity"), 0.0)
        aperture = _as_str(_payload_get(payload, "Aperture"), "")
        detector_gain = _as_float(_payload_get(payload, "DetectorGain"), 0.0)

        if experiment_file:
            config.experiment_file = experiment_file
            # Vendor XPM workflow: 0 means omit so the experiment file owns the value.
            config.resolution = resolution if resolution > 0 else 0.0
            config.sample_scans = sample_scans if sample_scans and sample_scans > 0 else 0
            config.background_scans = background_scans if background_scans > 0 else 0
        else:
            if resolution > 0:
                config.resolution = resolution
            if sample_scans and sample_scans > 0:
                config.sample_scans = sample_scans
            if background_scans > 0:
                config.background_scans = background_scans
        if output_path:
            config.output_path = output_path
        if reference_snm:
            config.sample_name = "Reference"
        elif sample_name:
            config.sample_name = sample_name

        extra = _empty_object(
            _payload_get(payload, "ExtraParameters", "ExtraParametersJson", default={})
        )
        if optical_velocity > 0:
            extra["VEL"] = optical_velocity
        if aperture:
            extra["APT"] = aperture
        if detector_gain > 0:
            extra[gain_opus_key] = detector_gain
        if extra:
            config.extra = extra
        return config

    def _move_to_well_if_requested(
        self, well: str, *, correlation_id: str = "", plate_format: int = 0
    ) -> dict[str, Any] | None:
        """Move to *well* when non-empty; return an error envelope dict or None."""
        if not well:
            return None

        def _call() -> Any:
            ctrl = self._exec.ftir.hts_xt()
            return ctrl.move_to_well(
                well, plate_format=plate_format if plate_format > 0 else None
            )

        _pos, err = _session_result(
            self._exec,
            _call,
            label=f"MoveToWell {well}",
            correlation_id=correlation_id,
        )
        return err

    def MeasureBackground(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            MeasureBackground_IntermediateResponses
        ],
    ) -> MeasureBackground_Responses:
        req, _bad_req = _require_request(Request, MeasureBackground_Responses)
        if _bad_req is not None:
            return _bad_req
        _set_lifetime(instance, req.timeout_seconds)
        try:
            typed = _typed_payload(payloads.MeasureBackgroundRequestPayload, req.payload)
            payload = typed.as_dict()
            config = self._build_config_from_payload(
                payload, sample_scans=0, gain_opus_key="RGN", reference_snm=True
            )
            well = _as_str(_payload_get(payload, "Well"), "")
        except (ValueError, TypeError, KeyError) as e:
            return MeasureBackground_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )

        move_err = self._move_to_well_if_requested(well, correlation_id=req.correlation_id)
        if move_err is not None:
            return MeasureBackground_Responses(Result=move_err)

        def on_progress(percent: int, status: str) -> None:
            _emit_measure_update(
                instance,
                MeasureBackground_IntermediateResponses,
                percent,
                status,
                correlation_id=req.correlation_id,
            )

        ok, err = _session_result(
            self._exec,
            self._exec.ftir.measure_background,
            config,
            on_progress=on_progress,
            label="MeasureBackground",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return MeasureBackground_Responses(Result=err)
        if not ok:
            return MeasureBackground_Responses(
                Result=failed(
                    code=Code.OPUS_MEASUREMENT_FAILED,
                    message="OPUS MeasureBackground reported failure",
                    payload=payloads.MeasureBackgroundResult(Success=False),
                    correlation_id=req.correlation_id,
                )
            )
        return MeasureBackground_Responses(
            Result=succeeded(
                payload=payloads.MeasureBackgroundResult(Success=True),
                correlation_id=req.correlation_id,
            )
        )

    def MeasureSample(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            MeasureSample_IntermediateResponses
        ],
    ) -> MeasureSample_Responses:
        req, _bad_req = _require_request(Request, MeasureSample_Responses)
        if _bad_req is not None:
            return _bad_req
        _set_lifetime(instance, req.timeout_seconds)
        try:
            payload = _payload_mapping(req.payload)
            sample_name = _as_str(_payload_get(payload, "SampleName"), "")
            config = self._build_config_from_payload(
                payload, gain_opus_key="SGN", sample_name=sample_name
            )
            include_data = _as_bool(_payload_get(payload, "IncludeData"), False)
            export_asm = _as_bool(_payload_get(payload, "ExportAsm"), True)
            asm_output_path = _as_str(_payload_get(payload, "AsmOutputPath"), "")
            well = _as_str(_payload_get(payload, "Well"), "")
            operator = _as_str(_payload_get(payload, "Operator"), "")
            project = _as_str(_payload_get(payload, "Project"), "")
            asm_metadata = _empty_object(
                _payload_get(payload, "AsmMetadata", "AsmMetadataJson", default={})
            )
        except (ValueError, TypeError, KeyError) as e:
            return MeasureSample_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )

        move_err = self._move_to_well_if_requested(well, correlation_id=req.correlation_id)
        if move_err is not None:
            return MeasureSample_Responses(Result=move_err)

        def on_progress(percent: int, status: str) -> None:
            _emit_measure_update(
                instance,
                MeasureSample_IntermediateResponses,
                percent,
                status,
                correlation_id=req.correlation_id,
            )

        result, err = _session_result(
            self._exec,
            self._exec.ftir.measure,
            config,
            export_asm=export_asm,
            asm_path=asm_output_path,
            sample_name=sample_name,
            operator=operator,
            project=project,
            asm_metadata=asm_metadata or None,
            on_progress=on_progress,
            label="MeasureSample",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return MeasureSample_Responses(Result=err)
        assert result is not None
        measure_payload = _measure_sample_payload(
            file_path=result.file_path,
            asm_path=result.asm_path,
            spectra=result.spectra,
            raw_response=result.raw_response,
            include_data=include_data,
        )
        if not result.success:
            return MeasureSample_Responses(
                Result=failed(
                    code=Code.OPUS_MEASUREMENT_FAILED,
                    message="OPUS MeasureSample reported failure",
                    payload=measure_payload,
                    details={"RawResponse": measure_payload.RawResponse},
                    correlation_id=req.correlation_id,
                )
            )
        if result.asm_path:
            self._record_asm_context(result.asm_path)
        return MeasureSample_Responses(
            Result=succeeded(payload=measure_payload, correlation_id=req.correlation_id)
        )

    def ReadSpectrum(self, Request: Any, *, metadata: MetadataDict) -> ReadSpectrum_Responses:
        req, _bad_req = _require_request(Request, ReadSpectrum_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.ReadSpectrumRequestPayload, req.payload)
            file_path = str(typed.FilePath or "")
            include_data = bool(typed.IncludeData)
            if not file_path:
                raise ValueError("Payload.FilePath is required")
        except (ValueError, TypeError) as e:
            return ReadSpectrum_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        spectra, err = _session_result(
            self._exec,
            self._exec.ftir.read_spectrum,
            file_path,
            label="ReadSpectrum",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return ReadSpectrum_Responses(Result=err)
        assert spectra is not None
        return ReadSpectrum_Responses(
            Result=succeeded(
                payload=payloads.ReadSpectrumResult(
                    Spectra=_spectra_blocks(spectra, include_data)
                ),
                correlation_id=req.correlation_id,
            )
        )

    def ReadParameters(self, Request: Any, *, metadata: MetadataDict) -> ReadParameters_Responses:
        req, _bad_req = _require_request(Request, ReadParameters_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.ReadParametersRequestPayload, req.payload)
            file_path = str(typed.FilePath or "")
            if not file_path:
                raise ValueError("Payload.FilePath is required")
        except (ValueError, TypeError) as e:
            return ReadParameters_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        params, err = _session_result(
            self._exec,
            self._exec.ftir.read_parameters,
            file_path,
            label="ReadParameters",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return ReadParameters_Responses(Result=err)
        return ReadParameters_Responses(
            Result=succeeded(
                payload=payloads.ReadParametersResult(Parameters=params),
                correlation_id=req.correlation_id,
            )
        )

    def ExportAsm(self, Request: Any, *, metadata: MetadataDict) -> ExportAsm_Responses:
        req, _bad_req = _require_request(Request, ExportAsm_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            payload = _payload_mapping(req.payload)
            file_path = _as_str(_payload_get(payload, "FilePath"), "")
            asm_output_path = _as_str(_payload_get(payload, "AsmOutputPath"), "")
            sample_name = _as_str(_payload_get(payload, "SampleName"), "")
            operator = _as_str(_payload_get(payload, "Operator"), "")
            project = _as_str(_payload_get(payload, "Project"), "")
            asm_metadata = _empty_object(
                _payload_get(payload, "AsmMetadata", "AsmMetadataJson", default={})
            )
            if not file_path:
                raise ValueError("Payload.FilePath is required")
        except (ValueError, TypeError) as e:
            return ExportAsm_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        path, err = _session_result(
            self._exec,
            self._exec.ftir.export_asm,
            file_path,
            asm_output_path,
            sample_name,
            operator=operator,
            project=project,
            asm_metadata=asm_metadata or None,
            label="ExportAsm",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return ExportAsm_Responses(Result=err)
        assert path is not None
        self._record_asm_context(path)
        return ExportAsm_Responses(
            Result=succeeded(
                payload=payloads.ExportAsmResult(AsmPath=path),
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# StageControlService
# ---------------------------------------------------------------------------


class StageControlServiceImpl(StageControlServiceBase):
    """Stage / plate-transport control over the curated instrument bindings."""

    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self._exec = executor

    def _run_stage(
        self, method_name: str, *, correlation_id: str = "", **opus_params: Any
    ) -> tuple[list | None, dict[str, Any] | None]:
        kwargs = {k: v for k, v in opus_params.items() if v != ""}

        def _call() -> Any:
            fn = getattr(self._exec.ftir.instrument, method_name)
            return fn(**kwargs)

        response, err = _session_result(
            self._exec,
            _call,
            label=f"StageControl {method_name}",
            correlation_id=correlation_id,
        )
        if err is not None:
            return None, err
        tel = get_telemetry()
        if method_name == "open_drawer":
            tel.record_drawer(sc.ACTION_OPEN)
        elif method_name == "close_drawer":
            tel.record_drawer(sc.ACTION_CLOSE)
        elif method_name in {
            "move_stage",
            "move_stage_to_reference_position",
            "set_reference_position",
        }:
            tel.record_stage_move()
        return list(response or []), None

    def MoveStage(self, Request: Any, *, metadata: MetadataDict) -> MoveStage_Responses:
        req, _bad_req = _require_request(Request, MoveStage_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.MoveStageRequestPayload, req.payload)
            device = str(typed.Device or "")
            x = float(typed.X)
            y = float(typed.Y)
        except (ValueError, TypeError) as e:
            return MoveStage_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "move_stage", correlation_id=req.correlation_id, NDV=device, NOX=x, NOY=y
        )
        if err is not None:
            return MoveStage_Responses(Result=err)
        return MoveStage_Responses(
            Result=succeeded(
                payload=_drawer_payload("move_stage", response),
                correlation_id=req.correlation_id,
            )
        )

    def MoveToWell(self, Request: Any, *, metadata: MetadataDict) -> MoveToWell_Responses:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            well = _as_str(_payload_get(payload, "Well"), "")
            plate_format = _as_int(_payload_get(payload, "PlateFormat"), 0)
            if not well:
                raise ValueError("Payload.Well is required")
        except (ValueError, TypeError) as e:
            return MoveToWell_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )

        def _call() -> Any:
            ctrl = self._exec.ftir.hts_xt()
            pos = ctrl.move_to_well(
                well, plate_format=plate_format if plate_format > 0 else None
            )
            return [pos.well, str(pos.x_um), str(pos.y_um)]

        response, err = _session_result(
            self._exec,
            _call,
            label=f"StageControl MoveToWell {well}",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return MoveToWell_Responses(Result=err)
        get_telemetry().record_stage_move()
        return MoveToWell_Responses(
            Result=succeeded(
                payload=_drawer_payload("move_to_well", response),
                correlation_id=req.correlation_id,
            )
        )

    def LoadPlate(self, Request: Any, *, metadata: MetadataDict) -> LoadPlate_Responses:
        req = parse_request(Request)

        def _call() -> Any:
            return self._exec.ftir.hts_xt().load_plate()

        response, err = _session_result(
            self._exec,
            _call,
            label="StageControl LoadPlate",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return LoadPlate_Responses(Result=err)
        get_telemetry().record_stage_move()
        return LoadPlate_Responses(
            Result=succeeded(
                payload=_drawer_payload("load_plate", response),
                correlation_id=req.correlation_id,
            )
        )

    def UnloadPlate(self, Request: Any, *, metadata: MetadataDict) -> UnloadPlate_Responses:
        req = parse_request(Request)

        def _call() -> Any:
            return self._exec.ftir.hts_xt().unload_plate()

        response, err = _session_result(
            self._exec,
            _call,
            label="StageControl UnloadPlate",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return UnloadPlate_Responses(Result=err)
        get_telemetry().record_stage_move()
        return UnloadPlate_Responses(
            Result=succeeded(
                payload=_drawer_payload("unload_plate", response),
                correlation_id=req.correlation_id,
            )
        )

    def OpenDrawer(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            OpenDrawer_IntermediateResponses
        ],
    ) -> OpenDrawer_Responses:
        req, _bad_req = _require_request(Request, OpenDrawer_Responses)
        if _bad_req is not None:
            return _bad_req
        _set_lifetime(instance, req.timeout_seconds)
        instance.send_intermediate_response(
            OpenDrawer_IntermediateResponses(
                Update=running(
                    payload=payloads.DrawerProgress(Phase="running"),
                    correlation_id=req.correlation_id,
                )
            )
        )
        response, err = self._run_stage("open_drawer", correlation_id=req.correlation_id)
        if err is not None:
            return OpenDrawer_Responses(Result=err)
        return OpenDrawer_Responses(
            Result=succeeded(
                payload=_drawer_payload("open_drawer", response),
                correlation_id=req.correlation_id,
            )
        )

    def CloseDrawer(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[
            CloseDrawer_IntermediateResponses
        ],
    ) -> CloseDrawer_Responses:
        req, _bad_req = _require_request(Request, CloseDrawer_Responses)
        if _bad_req is not None:
            return _bad_req
        _set_lifetime(instance, req.timeout_seconds)
        instance.send_intermediate_response(
            CloseDrawer_IntermediateResponses(
                Update=running(
                    payload=payloads.DrawerProgress(Phase="running"),
                    correlation_id=req.correlation_id,
                )
            )
        )
        response, err = self._run_stage("close_drawer", correlation_id=req.correlation_id)
        if err is not None:
            return CloseDrawer_Responses(Result=err)
        return CloseDrawer_Responses(
            Result=succeeded(
                payload=_drawer_payload("close_drawer", response),
                correlation_id=req.correlation_id,
            )
        )

    def MoveStageToReferencePosition(
        self, Request: Any, *, metadata: MetadataDict
    ) -> MoveStageToReferencePosition_Responses:
        req, _bad_req = _require_request(Request, MoveStageToReferencePosition_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(
                payloads.MoveStageToReferencePositionRequestPayload, req.payload
            )
            device = str(typed.Device or "")
            reference_position = str(typed.ReferencePosition or "")
        except (ValueError, TypeError) as e:
            return MoveStageToReferencePosition_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "move_stage_to_reference_position",
            correlation_id=req.correlation_id,
            NDV=device,
            RPI=reference_position,
        )
        if err is not None:
            return MoveStageToReferencePosition_Responses(Result=err)
        return MoveStageToReferencePosition_Responses(
            Result=succeeded(
                payload=_drawer_payload("move_stage_to_reference_position", response),
                correlation_id=req.correlation_id,
            )
        )

    def GetStagePosition(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetStagePosition_Responses:
        req, _bad_req = _require_request(Request, GetStagePosition_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.DeviceRequestPayload, req.payload)
            device = str(typed.Device or "")
        except (ValueError, TypeError) as e:
            return GetStagePosition_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "get_stage_position", correlation_id=req.correlation_id, NDV=device
        )
        if err is not None:
            return GetStagePosition_Responses(Result=err)
        return GetStagePosition_Responses(
            Result=succeeded(
                payload=_drawer_payload("get_stage_position", response),
                correlation_id=req.correlation_id,
            )
        )

    def GetStageName(self, Request: Any, *, metadata: MetadataDict) -> GetStageName_Responses:
        req, _bad_req = _require_request(Request, GetStageName_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.GetStageNameRequestPayload, req.payload)
            imaging_device = str(typed.ImagingDevice or "")
        except (ValueError, TypeError) as e:
            return GetStageName_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "get_stage_name", correlation_id=req.correlation_id, IDV=imaging_device
        )
        if err is not None:
            return GetStageName_Responses(Result=err)
        return GetStageName_Responses(
            Result=succeeded(
                payload=_drawer_payload("get_stage_name", response),
                correlation_id=req.correlation_id,
            )
        )

    def GetNumberOfAxes(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetNumberOfAxes_Responses:
        req, _bad_req = _require_request(Request, GetNumberOfAxes_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.DeviceRequestPayload, req.payload)
            device = str(typed.Device or "")
        except (ValueError, TypeError) as e:
            return GetNumberOfAxes_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "get_number_of_axes", correlation_id=req.correlation_id, NDV=device
        )
        if err is not None:
            return GetNumberOfAxes_Responses(Result=err)
        return GetNumberOfAxes_Responses(
            Result=succeeded(
                payload=_drawer_payload("get_number_of_axes", response),
                correlation_id=req.correlation_id,
            )
        )

    def SetReferencePosition(
        self, Request: Any, *, metadata: MetadataDict
    ) -> SetReferencePosition_Responses:
        req, _bad_req = _require_request(Request, SetReferencePosition_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.SetReferencePositionRequestPayload, req.payload)
            device = str(typed.Device or "")
            reference_position = str(typed.ReferencePosition or "")
            x = float(typed.X)
            y = float(typed.Y)
            z = float(typed.Z)
        except (ValueError, TypeError) as e:
            return SetReferencePosition_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "set_reference_position",
            correlation_id=req.correlation_id,
            NDV=device,
            RPI=reference_position,
            RXC=x,
            RYC=y,
            RZC=z,
        )
        if err is not None:
            return SetReferencePosition_Responses(Result=err)
        return SetReferencePosition_Responses(
            Result=succeeded(
                payload=_drawer_payload("set_reference_position", response),
                correlation_id=req.correlation_id,
            )
        )

    def IsReferencePlateAvailable(
        self, Request: Any, *, metadata: MetadataDict
    ) -> IsReferencePlateAvailable_Responses:
        req, _bad_req = _require_request(Request, IsReferencePlateAvailable_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.DeviceRequestPayload, req.payload)
            device = str(typed.Device or "")
        except (ValueError, TypeError) as e:
            return IsReferencePlateAvailable_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        response, err = self._run_stage(
            "is_reference_plate_available",
            correlation_id=req.correlation_id,
            NDV=device,
        )
        if err is not None:
            return IsReferencePlateAvailable_Responses(Result=err)
        return IsReferencePlateAvailable_Responses(
            Result=succeeded(
                payload=_drawer_payload("is_reference_plate_available", response),
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# OpusFunctionService
# ---------------------------------------------------------------------------


class OpusFunctionServiceImpl(OpusFunctionServiceBase):
    def __init__(self, parent_server: SilaServer, executor: FtirExecutor) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._meta: OpusFunctionsProxy | None = None

    def _metadata(self) -> OpusFunctionsProxy:
        if self._meta is None:
            self._meta = _metadata_proxy()
        return self._meta

    def ExecuteFunction(
        self, Request: Any, *, metadata: MetadataDict
    ) -> ExecuteFunction_Responses:
        req, _bad_req = _require_request(Request, ExecuteFunction_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.ExecuteFunctionRequestPayload, req.payload)
            function_name = str(typed.FunctionName or "")
            file_id = int(typed.FileId or 0)
            block_name = str(typed.BlockName or "")
            params = _empty_object(typed.Parameters)
            if not function_name:
                raise ValueError("Payload.FunctionName is required")
        except (ValueError, TypeError) as e:
            return ExecuteFunction_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )

        kwargs: dict[str, Any] = dict(params)
        if file_id > 0:
            kwargs["file_id"] = file_id
        if block_name:
            kwargs["block_name"] = block_name

        def _call() -> Any:
            try:
                fn = getattr(self._exec.ftir.opus, function_name)
            except AttributeError as e:
                raise _UnknownOpusFunction(str(e)) from e
            return fn(**kwargs)

        try:
            response, err = _session_result(
                self._exec,
                _call,
                label=f"ExecuteFunction {function_name}",
                correlation_id=req.correlation_id,
                reraise=(_UnknownOpusFunction,),
            )
        except _UnknownOpusFunction as e:
            return ExecuteFunction_Responses(
                Result=rejected(
                    code=Code.OPUS_UNKNOWN_FUNCTION,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        if err is not None:
            return ExecuteFunction_Responses(Result=err)
        return ExecuteFunction_Responses(
            Result=succeeded(
                payload=payloads.ExecuteFunctionResult(
                    Function=function_name,
                    Response=[str(r) for r in (response or [])],
                ),
                correlation_id=req.correlation_id,
            )
        )

    def ListFunctions(self, Request: Any, *, metadata: MetadataDict) -> ListFunctions_Responses:
        req, _bad_req = _require_request(Request, ListFunctions_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            names = self._metadata().list_functions()
        except Exception as e:
            return ListFunctions_Responses(
                Result=failed(
                    code=Code.OPUS_API,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        return ListFunctions_Responses(
            Result=succeeded(
                payload=payloads.ListFunctionsResult(Names=list(names)),
                correlation_id=req.correlation_id,
            )
        )

    def DescribeFunction(
        self, Request: Any, *, metadata: MetadataDict
    ) -> DescribeFunction_Responses:
        req, _bad_req = _require_request(Request, DescribeFunction_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.DescribeFunctionRequestPayload, req.payload)
            function_name = str(typed.FunctionName or "")
            if not function_name:
                raise ValueError("Payload.FunctionName is required")
        except (ValueError, TypeError) as e:
            return DescribeFunction_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        try:
            info = self._metadata().describe(function_name)
        except AttributeError as e:
            return DescribeFunction_Responses(
                Result=rejected(
                    code=Code.OPUS_UNKNOWN_FUNCTION,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        except Exception as e:
            return DescribeFunction_Responses(
                Result=failed(
                    code=Code.OPUS_API,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        return DescribeFunction_Responses(
            Result=succeeded(
                payload=payloads.DescribeFunctionResult(
                    Function=str(info.get("function") or function_name),
                    InputParameters=[str(x) for x in (info.get("input_parameters") or [])],
                    ReturnParameters=[str(x) for x in (info.get("return_parameters") or [])],
                    ParameterDetails=info.get("parameter_details") or {},
                ),
                correlation_id=req.correlation_id,
            )
        )

    def RawCommand(self, Request: Any, *, metadata: MetadataDict) -> RawCommand_Responses:
        req, _bad_req = _require_request(Request, RawCommand_Responses)
        if _bad_req is not None:
            return _bad_req
        try:
            typed = _typed_payload(payloads.RawCommandRequestPayload, req.payload)
            command = str(typed.Command or "")
            if not command:
                raise ValueError("Payload.Command is required")
        except (ValueError, TypeError) as e:
            return RawCommand_Responses(
                Result=rejected(
                    code=Code.REQUEST_INVALID_PAYLOAD,
                    message=str(e),
                    correlation_id=req.correlation_id,
                )
            )
        result, err = _session_result(
            self._exec,
            self._exec.ftir.send_raw_command,
            command,
            label="RawCommand",
            correlation_id=req.correlation_id,
        )
        if err is not None:
            return RawCommand_Responses(Result=err)
        assert result is not None
        success, response = result
        body = payloads.RawCommandResult(
            Success=bool(success),
            Response=[str(r) for r in list(response)],
        )
        if not success:
            return RawCommand_Responses(
                Result=failed(
                    code=Code.OPUS_API,
                    message="OPUS raw command reported failure",
                    payload=body,
                    correlation_id=req.correlation_id,
                )
            )
        return RawCommand_Responses(
            Result=succeeded(payload=body, correlation_id=req.correlation_id)
        )
