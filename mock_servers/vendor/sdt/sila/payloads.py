"""Typed payload structures for SDT command Request/Envelope.Payload.

Each class subclasses ``sila_common.envelope.AnyPayload``. On the wire,
Payload fields are UTF-8 JSON strings; these dataclasses are the in-memory
models. Matching ``DataTypeDefinition`` entries are generated in
``scripts/generate_sila_features.py``.

Opaque TRIOS REST JSON lives in a nested ``Body`` object. Known SiLA keys
(``QueueType``, ``ExperimentId``, …) are PascalCase fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sila_common.envelope import AnyPayload, structure_type_xml
from sila_common.fdl import basic_dt

# ---------------------------------------------------------------------------
# Shared element schemas
# ---------------------------------------------------------------------------

REST_BODY_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Body",
        "Body",
        "Opaque TRIOS REST JSON body. Void or empty object means none. "
        "When --payload-json omits Body, remaining keys are treated as the body.",
        basic_dt("Any"),
    ),
]

REST_BODY_RESULT_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Body",
        "Body",
        "Opaque TRIOS REST JSON response (or status snapshot).",
        basic_dt("Any"),
    ),
]

QUEUE_TYPE_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "QueueType",
        "Queue type",
        'design | running; empty/omit for server default.',
        basic_dt("String"),
    ),
]

QUEUE_BODY_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "QueueType",
        "Queue type",
        'design | running; empty/omit for server default.',
        basic_dt("String"),
    ),
    (
        "Body",
        "Body",
        "Opaque TRIOS REST JSON body. When --payload-json omits Body, remaining keys "
        "(except QueueType) are treated as the body.",
        basic_dt("Any"),
    ),
]

EXPERIMENT_ID_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "ExperimentId",
        "Experiment id",
        "Queued experiment identifier.",
        basic_dt("String"),
    ),
]

EXPERIMENT_BODY_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "ExperimentId",
        "Experiment id",
        "Queued experiment identifier.",
        basic_dt("String"),
    ),
    (
        "Body",
        "Body",
        "Opaque TRIOS REST JSON body. When --payload-json omits Body, remaining keys "
        "(except ExperimentId) are treated as the body.",
        basic_dt("Any"),
    ),
]

LOGIN_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Username", "Username", "TRIOS username.", basic_dt("String")),
    ("Password", "Password", "TRIOS password.", basic_dt("String")),
]

LIST_QUEUE_EXPERIMENTS_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Index",
        "Index",
        "1-based experiment index; 0/omit to list all.",
        basic_dt("Integer"),
    ),
    (
        "QueueType",
        "Queue type",
        'design | running; empty/omit for server default.',
        basic_dt("String"),
    ),
]

PARK_AUTOSAMPLER_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Position",
        "Position",
        "TraySide or FurnaceSide.",
        basic_dt("String"),
    ),
]

CONTROL_START_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "WaitForReady",
        "Wait for ready",
        'empty | "true" | "false"; empty means server default.',
        basic_dt("String"),
    ),
    (
        "ReadyTimeoutSeconds",
        "Ready timeout seconds",
        "REST ready-wait timeout; 0 omits.",
        basic_dt("Integer"),
    ),
    (
        "Force",
        "Force",
        "Retry with force after HTTP 409 / canForce.",
        basic_dt("Boolean"),
    ),
]

CONTROL_STOP_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Force",
        "Force",
        "Force stop after HTTP 409 / canForce.",
        basic_dt("Boolean"),
    ),
]

DELETE_REFERENCE_PAN_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "PanNumber",
        "Pan number",
        "Reference pan number to delete.",
        basic_dt("Integer"),
    ),
]

GET_EXPORT_JSON_JOB_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "JobId",
        "Job id",
        "Async export job identifier.",
        basic_dt("String"),
    ),
]

INSTRUMENT_STATUS_PROGRESS_ELEMENTS = REST_BODY_RESULT_ELEMENTS
AUTOSAMPLER_PROGRESS_ELEMENTS = REST_BODY_RESULT_ELEMENTS
QUEUE_STATUS_PROGRESS_ELEMENTS = REST_BODY_RESULT_ELEMENTS


# ---------------------------------------------------------------------------
# Shared payload classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestBodyRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(REST_BODY_REQUEST_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    Body: Any = None


@dataclass(frozen=True, slots=True)
class RestBodyResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(REST_BODY_RESULT_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    Body: Any = None


@dataclass(frozen=True, slots=True)
class QueueTypeRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(QUEUE_TYPE_REQUEST_ELEMENTS)
    QueueType: str = ""


@dataclass(frozen=True, slots=True)
class QueueBodyRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(QUEUE_BODY_REQUEST_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    QueueType: str = ""
    Body: Any = None


@dataclass(frozen=True, slots=True)
class ExperimentIdRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(EXPERIMENT_ID_REQUEST_ELEMENTS)
    ExperimentId: str = ""


@dataclass(frozen=True, slots=True)
class ExperimentBodyRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(EXPERIMENT_BODY_REQUEST_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    ExperimentId: str = ""
    Body: Any = None


@dataclass(frozen=True, slots=True)
class LoginRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(LOGIN_REQUEST_ELEMENTS)
    Username: str = ""
    Password: str = ""


@dataclass(frozen=True, slots=True)
class ListQueueExperimentsRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(LIST_QUEUE_EXPERIMENTS_REQUEST_ELEMENTS)
    Index: int = 0
    QueueType: str = ""


@dataclass(frozen=True, slots=True)
class ParkAutosamplerRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(PARK_AUTOSAMPLER_REQUEST_ELEMENTS)
    Position: str = ""


@dataclass(frozen=True, slots=True)
class ControlStartRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(CONTROL_START_REQUEST_ELEMENTS)
    WaitForReady: str = ""
    ReadyTimeoutSeconds: int = 0
    Force: bool = False


@dataclass(frozen=True, slots=True)
class ControlStopRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(CONTROL_STOP_REQUEST_ELEMENTS)
    Force: bool = False


@dataclass(frozen=True, slots=True)
class DeleteReferencePanRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(DELETE_REFERENCE_PAN_REQUEST_ELEMENTS)
    PanNumber: int = 0


@dataclass(frozen=True, slots=True)
class GetExportJsonJobRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(GET_EXPORT_JSON_JOB_REQUEST_ELEMENTS)
    JobId: str = ""


@dataclass(frozen=True, slots=True)
class InstrumentStatusProgress(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(INSTRUMENT_STATUS_PROGRESS_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    Body: Any = None


@dataclass(frozen=True, slots=True)
class AutosamplerProgress(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(AUTOSAMPLER_PROGRESS_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    Body: Any = None


@dataclass(frozen=True, slots=True)
class QueueStatusProgress(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(QUEUE_STATUS_PROGRESS_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Body"})
    Body: Any = None


# ---------------------------------------------------------------------------
# Per-command aliases (unique FDL identifiers; shared Python shapes)
# ---------------------------------------------------------------------------

# InstrumentStatus
WaitUntilIdleResult = RestBodyResult
WaitForTestStartedResult = RestBodyResult
GetInstrumentErrorsResult = RestBodyResult
GetControlPanelSignalsResult = RestBodyResult

# FurnaceControl
OpenFurnaceResult = RestBodyResult
CloseFurnaceResult = RestBodyResult

# QueueManagement
GetQueueStatusJsonRequestPayload = QueueTypeRequestPayload
ValidateQueueJsonRequestPayload = QueueTypeRequestPayload
AddExperimentsFromSequenceRequestPayload = QueueBodyRequestPayload
ValidateExperimentPayloadRequestPayload = QueueBodyRequestPayload
AddNewExperimentsRequestPayload = QueueBodyRequestPayload
CopyExperimentToQueueRequestPayload = QueueBodyRequestPayload
GetExperimentDetailsRequestPayload = ExperimentIdRequestPayload
ModifyExperimentInQueueRequestPayload = ExperimentBodyRequestPayload
RemoveExperimentFromQueueRequestPayload = ExperimentIdRequestPayload
MoveExperimentInQueueRequestPayload = ExperimentBodyRequestPayload
ValidateQueuedExperimentRequestPayload = ExperimentIdRequestPayload
ListQueueExperimentsResult = RestBodyResult
GetQueueStatusJsonResult = RestBodyResult
ValidateQueueJsonResult = RestBodyResult
AddExperimentsFromSequenceResult = RestBodyResult
ValidateExperimentPayloadResult = RestBodyResult
AddNewExperimentsResult = RestBodyResult
CopyExperimentToQueueResult = RestBodyResult
GetExperimentDetailsResult = RestBodyResult
ModifyExperimentInQueueResult = RestBodyResult
RemoveExperimentFromQueueResult = RestBodyResult
MoveExperimentInQueueResult = RestBodyResult
ValidateQueuedExperimentResult = RestBodyResult

# AutosamplerControl
GetAutosamplerSettingsJsonResult = RestBodyResult
UpdateAutosamplerSettingsRequestPayload = RestBodyRequestPayload
UpdateAutosamplerSettingsResult = RestBodyResult
LoadSamplePanRequestPayload = RestBodyRequestPayload
UnloadSamplePanRequestPayload = RestBodyRequestPayload
LoadReferencePanRequestPayload = RestBodyRequestPayload
UnloadReferencePanRequestPayload = RestBodyRequestPayload
LoadSamplePanResult = RestBodyResult
UnloadSamplePanResult = RestBodyResult
LoadReferencePanResult = RestBodyResult
UnloadReferencePanResult = RestBodyResult
ResetAutosamplerResult = RestBodyResult
ParkAutosamplerResult = RestBodyResult

# PanWeighing
GetTarePansJsonResult = RestBodyResult
StartTarePansRequestPayload = RestBodyRequestPayload
StartTarePansResult = RestBodyResult
GetPreWeighPansJsonResult = RestBodyResult
StartPreWeighPansRequestPayload = RestBodyRequestPayload
StartPreWeighPansResult = RestBodyResult

# ReferencePansManagement
GetReferencePansJsonResult = RestBodyResult
PutReferencePansRequestPayload = RestBodyRequestPayload
PutReferencePansResult = RestBodyResult
DeleteReferencePanResult = RestBodyResult

# SampleMode
GetSampleModeJsonResult = RestBodyResult
PutSampleModeRequestPayload = RestBodyRequestPayload
PutSampleModeResult = RestBodyResult

# GasControl
SelectGasRequestPayload = RestBodyRequestPayload
SelectGasResult = RestBodyResult
SetGasFlowRateRequestPayload = RestBodyRequestPayload
SetGasFlowRateResult = RestBodyResult

# Authentication
LoginResult = RestBodyResult
RefreshAuthTokenJsonRequestPayload = RestBodyRequestPayload
RefreshAuthTokenJsonResult = RestBodyResult
VerifyAuthTokenJsonRequestPayload = RestBodyRequestPayload
VerifyAuthTokenJsonResult = RestBodyResult

# Discovery
GetHealthJsonResult = RestBodyResult
ListInstancesJsonResult = RestBodyResult
GetInstrumentInfoJsonResult = RestBodyResult

# RunControl
ControlStartResult = RestBodyResult
ControlStopResult = RestBodyResult

# ResultsExport
ExportResultsJsonRequestPayload = RestBodyRequestPayload
ExportResultsJsonResult = RestBodyResult
GetExportJsonJobResult = RestBodyResult

# ApplicationControl
ShowTriosResult = RestBodyResult
HideTriosResult = RestBodyResult
GetUiEnabledResult = RestBodyResult
SetUiEnabledRequestPayload = RestBodyRequestPayload
SetUiEnabledResult = RestBodyResult

# ApplicationSettings
GetAnalysisSettingsResult = RestBodyResult
SetAnalysisSettingsRequestPayload = RestBodyRequestPayload
SetAnalysisSettingsResult = RestBodyResult
GetJsonSettingsResult = RestBodyResult
SetJsonSettingsRequestPayload = RestBodyRequestPayload
SetJsonSettingsResult = RestBodyResult
GetLimsSettingsResult = RestBodyResult
SetLimsSettingsRequestPayload = RestBodyRequestPayload
SetLimsSettingsResult = RestBodyResult
GetUserSettingsResult = RestBodyResult
SetUserSettingsRequestPayload = RestBodyRequestPayload
SetUserSettingsResult = RestBodyResult

# DialogControl
ShowMessageRequestPayload = RestBodyRequestPayload
ShowMessageResult = RestBodyResult
ShowNotificationRequestPayload = RestBodyRequestPayload
ShowNotificationResult = RestBodyResult
