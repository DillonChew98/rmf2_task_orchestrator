"""TriosX3dscClient-backed SiLA feature implementations (org.tainstruments / xdsc)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from queue import Queue
from typing import Any, Optional

from sila2.server import MetadataDict, ObservableCommandInstanceWithIntermediateResponses

from sila_common.envelope import AnyPayload, Code, Outcome, parse_envelope, parse_request, rejected
from trios_common.sila import (
    envelope_call as _shared_envelope_call,
    map_trios,
    poll_autosampler_with_envelope,
    poll_experiment_with_envelope,
    run_wait_with_envelope_stream,
    start_status_property_poller as _shared_start_status_property_poller,
    trios_error_envelope,
)

from sila import payloads
from x3dsc_automation import TriosX3dscClient, TriosX3dscClientError

from .authentication.authentication_base import AuthenticationBase
from .authentication.authentication_types import Login_Responses, RefreshAuthTokenJson_Responses, VerifyAuthTokenJson_Responses

from .autosamplercontrol.autosamplercontrol_base import AutosamplerControlBase
from .autosamplercontrol.autosamplercontrol_types import (
    CloseAutosamplerLid_IntermediateResponses,
    CloseAutosamplerLid_Responses,
    GetAutosamplerSettingsJson_Responses,
    LoadReferencePan_IntermediateResponses,
    LoadReferencePan_Responses,
    LoadSamplePan_IntermediateResponses,
    LoadSamplePan_Responses,
    OpenAutosamplerLid_IntermediateResponses,
    OpenAutosamplerLid_Responses,
    ParkAutosampler_IntermediateResponses,
    ParkAutosampler_Responses,
    UnloadReferencePan_IntermediateResponses,
    UnloadReferencePan_Responses,
    UnloadSamplePan_IntermediateResponses,
    UnloadSamplePan_Responses,
    UpdateAutosamplerSettings_Responses,
)

from .discovery.discovery_base import DiscoveryBase
from .discovery.discovery_types import GetHealthJson_Responses, GetInstrumentInfoJson_Responses, ListInstancesJson_Responses

from .instrumentstatus.instrumentstatus_base import InstrumentStatusBase
from .instrumentstatus.instrumentstatus_types import (
    GetInstrumentErrors_Responses,
    WaitForTestStarted_IntermediateResponses,
    WaitForTestStarted_Responses,
    WaitUntilIdle_IntermediateResponses,
    WaitUntilIdle_Responses,
)

from .queuemanagement.queuemanagement_base import QueueManagementBase
from .queuemanagement.queuemanagement_types import (
    AddExperimentsFromSequence_Responses,
    AddNewExperiments_Responses,
    CopyExperimentToQueue_Responses,
    GetExperimentDetails_Responses,
    GetQueueStatusJson_Responses,
    ListQueueExperiments_Responses,
    ModifyExperimentInQueue_Responses,
    MoveExperimentInQueue_Responses,
    RemoveExperimentFromQueue_Responses,
    ValidateExperimentPayload_Responses,
    ValidateQueuedExperiment_Responses,
    ValidateQueueJson_Responses,
)

from .referencepansmanagement.referencepansmanagement_base import ReferencePansManagementBase
from .referencepansmanagement.referencepansmanagement_types import (
    DeleteReferencePan_Responses,
    GetReferencePansJson_Responses,
    PutReferencePans_Responses,
)

from .runcontrol.runcontrol_base import RunControlBase
from .runcontrol.runcontrol_types import ControlStart_IntermediateResponses, ControlStart_Responses, ControlStop_Responses

from .sensorcontrol.sensorcontrol_base import SensorControlBase
from .sensorcontrol.sensorcontrol_types import SetSensors_Responses

from .resultsexport.resultsexport_base import ResultsExportBase
from .resultsexport.resultsexport_types import ExportResultsJson_Responses, GetExportJsonJob_Responses

from .applicationcontrol.applicationcontrol_base import ApplicationControlBase
from .applicationcontrol.applicationcontrol_types import (
    GetUiEnabled_Responses,
    HideTrios_Responses,
    SetUiEnabled_Responses,
    ShowTrios_Responses,
)

from .applicationsettings.applicationsettings_base import ApplicationSettingsBase
from .applicationsettings.applicationsettings_types import (
    GetAnalysisSettings_Responses,
    GetJsonSettings_Responses,
    GetLimsSettings_Responses,
    GetUserSettings_Responses,
    SetAnalysisSettings_Responses,
    SetJsonSettings_Responses,
    SetLimsSettings_Responses,
    SetUserSettings_Responses,
)

from .dialogcontrol.dialogcontrol_base import DialogControlBase
from .dialogcontrol.dialogcontrol_types import ShowMessage_Responses, ShowNotification_Responses

from .authentication import authentication_errors as authentication_errors_module
from .autosamplercontrol import autosamplercontrol_errors as autosamplercontrol_errors_module
from .discovery import discovery_errors as discovery_errors_module
from .instrumentstatus import instrumentstatus_errors as instrumentstatus_errors_module
from .queuemanagement import queuemanagement_errors as queuemanagement_errors_module
from .referencepansmanagement import referencepansmanagement_errors as referencepansmanagement_errors_module
from .runcontrol import runcontrol_errors as runcontrol_errors_module
from .sensorcontrol import sensorcontrol_errors as sensorcontrol_errors_module
from .resultsexport import resultsexport_errors as resultsexport_errors_module
from .applicationcontrol import applicationcontrol_errors as applicationcontrol_errors_module
from .applicationsettings import applicationsettings_errors as applicationsettings_errors_module
from .dialogcontrol import dialogcontrol_errors as dialogcontrol_errors_module

from .codec import bool_opt, dumps_json, index_opt, queue_type_opt, sniff_bearer_token


def _wrap_rest_body(result: Any) -> payloads.RestBodyResult:
    return payloads.RestBodyResult(Body=result)


def _wrap_instrument_progress(result: Any) -> payloads.InstrumentStatusProgress:
    return payloads.InstrumentStatusProgress(Body=result)


def _wrap_autosampler_progress(result: Any) -> payloads.AutosamplerProgress:
    return payloads.AutosamplerProgress(Body=result)


def _wrap_queue_progress(result: Any) -> payloads.QueueStatusProgress:
    return payloads.QueueStatusProgress(Body=result)


def _envelope_call(
    client_fn: Callable[..., Any],
    response_cls: type,
    *,
    payload: Any = ...,
    correlation_id: str = "",
    wrap_payload: Callable[[Any], Any] | None = None,
) -> Any:
    return response_cls(
        Result=_shared_envelope_call(
            client_fn,
            payload=payload,
            correlation_id=correlation_id,
            client_error_cls=TriosX3dscClientError,
            wrap_payload=wrap_payload or _wrap_rest_body,
        )
    )


def _start_status_property_poller(
    fetch_json: Callable[[], str],
    queue: Queue[str],
    err_cls: type[Any],
) -> None:
    """Daemon thread that polls REST and pushes JSON into a subscription queue."""
    _shared_start_status_property_poller(
        fetch_json,
        queue,
        err_cls,
        client_error_cls=TriosX3dscClientError,
        thread_name="x3dsc-status-poller",
    )


def _payload_mapping(payload: Any) -> dict[str, Any]:
    """Normalize Request.Payload to a PascalCase field dict.

    Accepts Void/None, Key/Value Any maps, named Structure dicts, AnyPayload,
    and sila2 Structure NamedTuples.
    """
    if payload is None:
        return {}
    if isinstance(payload, AnyPayload):
        return payload.as_dict()
    if isinstance(payload, Mapping):
        return dict(payload)
    if hasattr(payload, "_asdict"):
        return dict(payload._asdict())
    raise ValueError(f"Request.Payload must be a mapping or Void; got {type(payload).__name__}")


def _rest_body(payload: Mapping[str, Any], *reserved: str) -> dict[str, Any]:
    """Extract REST JSON body from Structure ``Body`` or flat ``--payload-json``.

    If ``Body`` is present, it is the REST object (Void/empty → ``{}``).
    Otherwise all keys except *reserved* SiLA fields form the body.
    """
    data = dict(payload)
    if "Body" in data:
        body = data["Body"]
        if body is None or body == "" or body == {}:
            return {}
        if isinstance(body, Mapping):
            return dict(body)
        raise ValueError("Request.Payload.Body must be an object or Void.")
    for key in reserved:
        data.pop(key, None)
    return data


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Request.Payload must include a non-empty string {key}.")
    return value


def _rejected_payload(message: str, *, correlation_id: str = "") -> dict[str, Any]:
    return rejected(
        code=Code.REQUEST_INVALID_PAYLOAD,
        message=message,
        correlation_id=correlation_id,
    )


def _apply_lifetime(instance: Any, timeout_seconds: int | None) -> None:
    if timeout_seconds is not None and timeout_seconds > 0:
        instance.lifetime_of_execution = timedelta(seconds=timeout_seconds)


# ---------------------------------------------------------------------------
# InstrumentStatus
# ---------------------------------------------------------------------------

class InstrumentStatusImpl(InstrumentStatusBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def _fetch_instrument_status_json(self) -> str:
        return dumps_json(self._c.get_instrument_status())

    @property
    def current_CurrentInstrumentStatus(self) -> str:
        try:
            return self._fetch_instrument_status_json()
        except TriosX3dscClientError as e:
            raise map_trios(instrumentstatus_errors_module.InternalError, e) from e

    def CurrentInstrumentStatus_on_subscription(self, *, metadata: MetadataDict) -> Optional[Queue[str]]:
        queue: Queue[str] = Queue()
        _start_status_property_poller(
            self._fetch_instrument_status_json,
            queue,
            instrumentstatus_errors_module.InternalError,
        )
        return queue

    def _run_wait(
        self,
        Request: Any,
        wait_call: Callable[[Optional[int]], Any],
        instance: ObservableCommandInstanceWithIntermediateResponses[Any],
        intermediate_cls: type,
        response_cls: type,
    ) -> Any:
        req = parse_request(Request)
        if req.timeout_seconds is not None and req.timeout_seconds < 0:
            return response_cls(
                Result=_rejected_payload(
                    "TimeoutSeconds must be zero or a positive integer.",
                    correlation_id=req.correlation_id,
                )
            )
        _apply_lifetime(instance, req.timeout_seconds)
        return response_cls(
            Result=run_wait_with_envelope_stream(
                lambda: wait_call(req.timeout_seconds),
                self._c.get_instrument_status,
                instance,
                intermediate_cls,
                client_error_cls=TriosX3dscClientError,
                correlation_id=req.correlation_id,
                wrap_payload=_wrap_instrument_progress,
            )
        )

    def WaitUntilIdle(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[WaitUntilIdle_IntermediateResponses],
    ) -> WaitUntilIdle_Responses:
        return self._run_wait(
            Request,
            lambda timeout: self._c.wait_until_idle(timeout_seconds=timeout),
            instance,
            WaitUntilIdle_IntermediateResponses,
            WaitUntilIdle_Responses,
        )

    def WaitForTestStarted(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[WaitForTestStarted_IntermediateResponses],
    ) -> WaitForTestStarted_Responses:
        return self._run_wait(
            Request,
            lambda timeout: self._c.wait_for_test_started(timeout_seconds=timeout),
            instance,
            WaitForTestStarted_IntermediateResponses,
            WaitForTestStarted_Responses,
        )

    def GetInstrumentErrors(self, Request: Any, *, metadata: MetadataDict) -> GetInstrumentErrors_Responses:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_instrument_errors,
            GetInstrumentErrors_Responses,
            correlation_id=req.correlation_id,
        )


# ---------------------------------------------------------------------------
# QueueManagement
# ---------------------------------------------------------------------------

class QueueManagementImpl(QueueManagementBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def ListQueueExperiments(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            index = index_opt(int(payload.get("Index", 0) or 0))
            queue_type = queue_type_opt(str(payload.get("QueueType", "") or ""))
        except (TypeError, ValueError) as exc:
            return ListQueueExperiments_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.list_queue_experiments(index=index, queue_type=queue_type),
            ListQueueExperiments_Responses,
            correlation_id=req.correlation_id,
        )

    def GetQueueStatusJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            queue_type = queue_type_opt(str(payload.get("QueueType", "") or ""))
        except (TypeError, ValueError) as exc:
            return GetQueueStatusJson_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.get_queue_status(queue_type=queue_type),
            GetQueueStatusJson_Responses,
            correlation_id=req.correlation_id,
        )

    def ValidateQueueJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            queue_type = queue_type_opt(str(payload.get("QueueType", "") or ""))
        except (TypeError, ValueError) as exc:
            return ValidateQueueJson_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.validate_queue(queue_type=queue_type),
            ValidateQueueJson_Responses,
            correlation_id=req.correlation_id,
        )

    def _queue_body_call(
        self,
        Request: Any,
        response_cls: type,
        client_fn: Callable[[dict[str, Any], Optional[str]], Any],
    ) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            queue_type = queue_type_opt(str(payload.get("QueueType", "") or ""))
            body = _rest_body(payload, "QueueType")
        except (TypeError, ValueError) as exc:
            return response_cls(Result=_rejected_payload(str(exc), correlation_id=req.correlation_id))
        return _envelope_call(
            lambda: client_fn(body, queue_type),
            response_cls,
            correlation_id=req.correlation_id,
        )

    def AddExperimentsFromSequence(self, Request: Any, *, metadata: MetadataDict) -> Any:
        return self._queue_body_call(
            Request,
            AddExperimentsFromSequence_Responses,
            lambda body, queue_type: self._c.add_experiments_from_sequence_to_queue(body, queue_type=queue_type),
        )

    def ValidateExperimentPayload(self, Request: Any, *, metadata: MetadataDict) -> Any:
        return self._queue_body_call(
            Request,
            ValidateExperimentPayload_Responses,
            lambda body, queue_type: self._c.validate_experiment(body, queue_type=queue_type),
        )

    def AddNewExperiments(self, Request: Any, *, metadata: MetadataDict) -> Any:
        return self._queue_body_call(
            Request,
            AddNewExperiments_Responses,
            lambda body, queue_type: self._c.add_new_experiments_to_queue(body, queue_type=queue_type),
        )

    def CopyExperimentToQueue(self, Request: Any, *, metadata: MetadataDict) -> Any:
        return self._queue_body_call(
            Request,
            CopyExperimentToQueue_Responses,
            lambda body, queue_type: self._c.copy_experiment_to_queue(body, queue_type=queue_type),
        )

    def GetExperimentDetails(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            experiment_id = _require_str(_payload_mapping(req.payload), "ExperimentId")
        except ValueError as exc:
            return GetExperimentDetails_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.get_experiment_details(experiment_id),
            GetExperimentDetails_Responses,
            correlation_id=req.correlation_id,
        )

    def ModifyExperimentInQueue(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            experiment_id = _require_str(payload, "ExperimentId")
            body = _rest_body(payload, "ExperimentId")
        except ValueError as exc:
            return ModifyExperimentInQueue_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.modify_experiment_in_queue(experiment_id, body),
            ModifyExperimentInQueue_Responses,
            correlation_id=req.correlation_id,
        )

    def RemoveExperimentFromQueue(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            experiment_id = _require_str(_payload_mapping(req.payload), "ExperimentId")
        except ValueError as exc:
            return RemoveExperimentFromQueue_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.remove_experiment_from_queue(experiment_id),
            RemoveExperimentFromQueue_Responses,
            correlation_id=req.correlation_id,
        )

    def MoveExperimentInQueue(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            experiment_id = _require_str(payload, "ExperimentId")
            body = _rest_body(payload, "ExperimentId")
        except ValueError as exc:
            return MoveExperimentInQueue_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.move_experiment_in_queue(experiment_id, body),
            MoveExperimentInQueue_Responses,
            correlation_id=req.correlation_id,
        )

    def ValidateQueuedExperiment(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            experiment_id = _require_str(_payload_mapping(req.payload), "ExperimentId")
        except ValueError as exc:
            return ValidateQueuedExperiment_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.validate_queued_experiment(experiment_id),
            ValidateQueuedExperiment_Responses,
            correlation_id=req.correlation_id,
        )


# ---------------------------------------------------------------------------
# ReferencePansManagement
# ---------------------------------------------------------------------------

class ReferencePansManagementImpl(ReferencePansManagementBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def GetReferencePansJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_reference_pans,
            GetReferencePansJson_Responses,
            correlation_id=req.correlation_id,
        )

    def PutReferencePans(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return PutReferencePans_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.put_reference_pans,
            PutReferencePans_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def DeleteReferencePan(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            pan_number = payload.get("PanNumber")
            if pan_number is None or pan_number == "":
                raise ValueError("Request.Payload must include PanNumber.")
        except ValueError as exc:
            return DeleteReferencePan_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.delete_reference_pan(pan_number),
            DeleteReferencePan_Responses,
            correlation_id=req.correlation_id,
        )


# ---------------------------------------------------------------------------
# AutosamplerControl
# ---------------------------------------------------------------------------

class AutosamplerControlImpl(AutosamplerControlBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def _fetch_autosampler_status_json(self) -> str:
        return dumps_json(self._c.get_autosampler_status())

    @property
    def current_CurrentAutosamplerStatus(self) -> str:
        try:
            return self._fetch_autosampler_status_json()
        except TriosX3dscClientError as e:
            raise map_trios(autosamplercontrol_errors_module.InternalError, e) from e

    def CurrentAutosamplerStatus_on_subscription(self, *, metadata: MetadataDict) -> Optional[Queue[str]]:
        queue: Queue[str] = Queue()
        _start_status_property_poller(
            self._fetch_autosampler_status_json,
            queue,
            autosamplercontrol_errors_module.InternalError,
        )
        return queue

    def _run_motion(
        self,
        Request: Any,
        start_motion: Callable[[], Any],
        instance: ObservableCommandInstanceWithIntermediateResponses[Any],
        intermediate_cls: type,
        response_cls: type,
    ) -> Any:
        req = parse_request(Request)
        _apply_lifetime(instance, req.timeout_seconds)
        try:
            start_motion()
        except TriosX3dscClientError as exc:
            return response_cls(Result=trios_error_envelope(exc, correlation_id=req.correlation_id))
        return response_cls(
            Result=poll_autosampler_with_envelope(
                self._c.get_autosampler_status,
                instance,
                intermediate_cls,
                correlation_id=req.correlation_id,
                client_error_cls=TriosX3dscClientError,
                wrap_payload=_wrap_autosampler_progress,
            )
        )

    def GetAutosamplerSettingsJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_autosampler_settings,
            GetAutosamplerSettingsJson_Responses,
            correlation_id=req.correlation_id,
        )

    def UpdateAutosamplerSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return UpdateAutosamplerSettings_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.update_autosampler_settings,
            UpdateAutosamplerSettings_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def LoadSamplePan(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[LoadSamplePan_IntermediateResponses],
    ) -> LoadSamplePan_Responses:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return LoadSamplePan_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return self._run_motion(
            Request,
            lambda: self._c.load_sample_pan(body),
            instance,
            LoadSamplePan_IntermediateResponses,
            LoadSamplePan_Responses,
        )

    def UnloadSamplePan(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[UnloadSamplePan_IntermediateResponses],
    ) -> UnloadSamplePan_Responses:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return UnloadSamplePan_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return self._run_motion(
            Request,
            lambda: self._c.unload_sample_pan(body if body else None),
            instance,
            UnloadSamplePan_IntermediateResponses,
            UnloadSamplePan_Responses,
        )

    def LoadReferencePan(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[LoadReferencePan_IntermediateResponses],
    ) -> LoadReferencePan_Responses:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return LoadReferencePan_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return self._run_motion(
            Request,
            lambda: self._c.load_reference_pan(body),
            instance,
            LoadReferencePan_IntermediateResponses,
            LoadReferencePan_Responses,
        )

    def UnloadReferencePan(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[UnloadReferencePan_IntermediateResponses],
    ) -> UnloadReferencePan_Responses:
        return self._run_motion(
            Request,
            self._c.unload_reference_pan,
            instance,
            UnloadReferencePan_IntermediateResponses,
            UnloadReferencePan_Responses,
        )

    def ParkAutosampler(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[ParkAutosampler_IntermediateResponses],
    ) -> ParkAutosampler_Responses:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            position = payload.get("Position") or payload.get("position")
            if not isinstance(position, str) or not position:
                raise ValueError("Request.Payload must include a non-empty string Position.")
        except ValueError as exc:
            return ParkAutosampler_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return self._run_motion(
            Request,
            lambda: self._c.park_autosampler({"position": position}),
            instance,
            ParkAutosampler_IntermediateResponses,
            ParkAutosampler_Responses,
        )

    def OpenAutosamplerLid(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[OpenAutosamplerLid_IntermediateResponses],
    ) -> OpenAutosamplerLid_Responses:
        return self._run_motion(
            Request,
            self._c.open_autosampler_lid,
            instance,
            OpenAutosamplerLid_IntermediateResponses,
            OpenAutosamplerLid_Responses,
        )

    def CloseAutosamplerLid(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[CloseAutosamplerLid_IntermediateResponses],
    ) -> CloseAutosamplerLid_Responses:
        return self._run_motion(
            Request,
            self._c.close_autosampler_lid,
            instance,
            CloseAutosamplerLid_IntermediateResponses,
            CloseAutosamplerLid_Responses,
        )


# ---------------------------------------------------------------------------
# SensorControl (X3-specific)
# ---------------------------------------------------------------------------

class SensorControlImpl(SensorControlBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def get_CurrentSensorsJson(self, *, metadata: MetadataDict) -> str:
        try:
            return dumps_json(self._c.get_sensors())
        except TriosX3dscClientError as e:
            raise map_trios(sensorcontrol_errors_module.InternalError, e) from e

    def SetSensors(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetSensors_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_sensors,
            SetSensors_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class AuthenticationImpl(AuthenticationBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def Login(self, Request: Any, *, metadata: MetadataDict) -> Login_Responses:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            username = _require_str(payload, "Username")
            password = _require_str(payload, "Password")
        except ValueError as exc:
            return Login_Responses(Result=_rejected_payload(str(exc), correlation_id=req.correlation_id))

        def _login() -> Any:
            result = self._c.login({"username": username, "password": password})
            token = sniff_bearer_token(result)
            if token:
                self._c.set_bearer_token(token)
            return result

        return _envelope_call(_login, Login_Responses, correlation_id=req.correlation_id)

    def RefreshAuthTokenJson(self, Request: Any, *, metadata: MetadataDict) -> RefreshAuthTokenJson_Responses:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return RefreshAuthTokenJson_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )

        def _refresh() -> Any:
            result = self._c.refresh_auth_token(body)
            token = sniff_bearer_token(result)
            if token:
                self._c.set_bearer_token(token)
            return result

        return _envelope_call(_refresh, RefreshAuthTokenJson_Responses, correlation_id=req.correlation_id)

    def VerifyAuthTokenJson(self, Request: Any, *, metadata: MetadataDict) -> VerifyAuthTokenJson_Responses:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return VerifyAuthTokenJson_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.verify_auth_token,
            VerifyAuthTokenJson_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class DiscoveryImpl(DiscoveryBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def GetHealthJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(self._c.get_health, GetHealthJson_Responses, correlation_id=req.correlation_id)

    def ListInstancesJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.list_instances, ListInstancesJson_Responses, correlation_id=req.correlation_id
        )

    def GetInstrumentInfoJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_instrument_info, GetInstrumentInfoJson_Responses, correlation_id=req.correlation_id
        )


# ---------------------------------------------------------------------------
# RunControl
# ---------------------------------------------------------------------------

class RunControlImpl(RunControlBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def ControlStart(
        self,
        Request: Any,
        *,
        metadata: MetadataDict,
        instance: ObservableCommandInstanceWithIntermediateResponses[ControlStart_IntermediateResponses],
    ) -> Any:
        req = parse_request(Request)
        _apply_lifetime(instance, req.timeout_seconds)
        try:
            payload = _payload_mapping(req.payload)
            wait_for_ready = bool_opt(str(payload.get("WaitForReady", "") or ""))
            ready_timeout = payload.get("ReadyTimeoutSeconds", 0)
            ready_timeout_seconds = None if not ready_timeout else int(ready_timeout)
            force = bool(payload.get("Force", False))
        except (TypeError, ValueError) as exc:
            return ControlStart_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )

        start_env = _shared_envelope_call(
            lambda: self._c.control_start(
                wait_for_ready=wait_for_ready,
                timeout_seconds=ready_timeout_seconds,
                force=force,
            ),
            correlation_id=req.correlation_id,
            client_error_cls=TriosX3dscClientError,
        )
        if parse_envelope(start_env).outcome != Outcome.SUCCEEDED:
            return ControlStart_Responses(Result=start_env)

        return ControlStart_Responses(
            Result=poll_experiment_with_envelope(
                self._c.get_queue_status,
                instance,
                ControlStart_IntermediateResponses,
                correlation_id=req.correlation_id,
                client_error_cls=TriosX3dscClientError,
                wrap_payload=_wrap_queue_progress,
            )
        )

    def ControlStop(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            payload = _payload_mapping(req.payload)
            force = bool(payload.get("Force", False))
        except (TypeError, ValueError) as exc:
            return ControlStop_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            lambda: self._c.control_stop(force=force),
            ControlStop_Responses,
            correlation_id=req.correlation_id,
        )


class ResultsExportImpl(ResultsExportBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def ExportResultsJson(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return ExportResultsJson_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.export_results_json,
            ExportResultsJson_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def GetExportJsonJob(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            job_id = _require_str(_payload_mapping(req.payload), "JobId")
        except ValueError as exc:
            return GetExportJsonJob_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        resp = _envelope_call(
            lambda: self._c.get_results_export_json_job(job_id),
            GetExportJsonJob_Responses,
            correlation_id=req.correlation_id,
        )
        try:
            from trios_common.harvest import maybe_harvest_export_job

            maybe_harvest_export_job(
                resp,
                run_id=job_id,
                instrument_id=getattr(self._c, "instrument_id", "") or "",
                instrument_kind="x3dsc",
            )
        except Exception:
            pass
        return resp


class ApplicationControlImpl(ApplicationControlBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def ShowTrios(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(self._c.show_trios, ShowTrios_Responses, correlation_id=req.correlation_id)

    def HideTrios(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(self._c.hide_trios, HideTrios_Responses, correlation_id=req.correlation_id)

    def GetUiEnabled(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(self._c.get_ui_enabled, GetUiEnabled_Responses, correlation_id=req.correlation_id)

    def SetUiEnabled(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetUiEnabled_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_ui_enabled,
            SetUiEnabled_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )


class ApplicationSettingsImpl(ApplicationSettingsBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def GetAnalysisSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_analysis_settings, GetAnalysisSettings_Responses, correlation_id=req.correlation_id
        )

    def SetAnalysisSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetAnalysisSettings_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_analysis_settings,
            SetAnalysisSettings_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def GetJsonSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_json_settings, GetJsonSettings_Responses, correlation_id=req.correlation_id
        )

    def SetJsonSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetJsonSettings_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_json_settings,
            SetJsonSettings_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def GetLimsSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_lims_settings, GetLimsSettings_Responses, correlation_id=req.correlation_id
        )

    def SetLimsSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetLimsSettings_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_lims_settings,
            SetLimsSettings_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def GetUserSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        return _envelope_call(
            self._c.get_user_settings, GetUserSettings_Responses, correlation_id=req.correlation_id
        )

    def SetUserSettings(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return SetUserSettings_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.set_user_settings,
            SetUserSettings_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )


class DialogControlImpl(DialogControlBase):

    def __init__(self, parent_server: Any, client: TriosX3dscClient) -> None:
        super().__init__(parent_server)
        self._c = client

    def ShowMessage(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return ShowMessage_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.show_message,
            ShowMessage_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )

    def ShowNotification(self, Request: Any, *, metadata: MetadataDict) -> Any:
        req = parse_request(Request)
        try:
            body = _rest_body(_payload_mapping(req.payload))
        except ValueError as exc:
            return ShowNotification_Responses(
                Result=_rejected_payload(str(exc), correlation_id=req.correlation_id)
            )
        return _envelope_call(
            self._c.show_notification,
            ShowNotification_Responses,
            payload=body,
            correlation_id=req.correlation_id,
        )
