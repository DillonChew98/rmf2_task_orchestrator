"""Typed payload structures for FTIR command Request/Envelope.Payload.

Each class subclasses ``sila_common.envelope.AnyPayload``. On the wire,
``Request.Payload`` / ``Envelope.Payload`` are UTF-8 JSON strings; these
dataclasses are the in-memory models. Matching ``DataTypeDefinition``
entries are generated in ``scripts/generate_sila_features.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from sila_common.envelope import AnyPayload, structure_type_xml
from sila_common.fdl import basic_dt, list_dt, structure_dt, type_id

# ---------------------------------------------------------------------------
# Shared nested structures
# ---------------------------------------------------------------------------

INTENSITY_TRACE_ELEMENTS: list[tuple[str, str, str, str]] = [
    (
        "Values",
        "Values",
        "Intensity samples for one spectral trace.",
        list_dt(basic_dt("Real")),
    ),
]

SPECTRUM_BLOCK_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("BlockName", "Block name", "OPUS block name, e.g. AB.", basic_dt("String")),
    ("NPoints", "N points", "Number of spectral data points.", basic_dt("Integer")),
    ("NTraces", "N traces", "Number of spectral traces (channels).", basic_dt("Integer")),
    ("WavenumberMin", "Wavenumber min", "Minimum wavenumber (cm-1).", basic_dt("Real")),
    ("WavenumberMax", "Wavenumber max", "Maximum wavenumber (cm-1).", basic_dt("Real")),
    ("FilePath", "File path", "Source OPUS file path.", basic_dt("String")),
    (
        "Wavenumbers",
        "Wavenumbers",
        "Wavenumber axis (cm-1); empty when IncludeData is false.",
        list_dt(basic_dt("Real")),
    ),
    (
        "Intensities",
        "Intensities",
        "Per-trace intensity series; empty when IncludeData is false.",
        list_dt(type_id("IntensityTrace")),
    ),
]

# Inline IntensityTrace for Any wire encoding (DataTypeIdentifier is FDL-only).
_SPECTRUM_BLOCK_ANY_ELEMENTS: list[tuple[str, str, str, str]] = [
    *SPECTRUM_BLOCK_ELEMENTS[:-1],
    (
        "Intensities",
        "Intensities",
        "Per-trace intensity series; empty when IncludeData is false.",
        list_dt(structure_dt(INTENSITY_TRACE_ELEMENTS)),
    ),
]

INSTRUMENT_SNAPSHOT_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Connected", "Connected", "Whether a working OPUS HTTP session is established.", basic_dt("Boolean")),
    ("OpusVersion", "OPUS version", 'OPUS version string, e.g. "9.2".', basic_dt("String")),
    (
        "OpusVersionExt",
        "OPUS version (extended)",
        "Extended OPUS version string with build details.",
        basic_dt("String"),
    ),
    ("HostIp", "Host IP", "IP address of the OPUS host as reported by OPUS.", basic_dt("String")),
    ("Language", "Language", 'OPUS UI language, e.g. "English".', basic_dt("String")),
]

DRAWER_RESULT_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Function", "Function", "OPUS / binding function name that was invoked.", basic_dt("String")),
    (
        "Response",
        "Response",
        "Raw OPUS response lines.",
        list_dt(basic_dt("String")),
    ),
]

MEASURE_PROGRESS_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Percent", "Percent", "Progress 0-100.", basic_dt("Integer")),
    ("Status", "Status", "Human-readable progress status from OPUS.", basic_dt("String")),
]

DRAWER_PROGRESS_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Phase", "Phase", "Drawer move phase, e.g. running.", basic_dt("String")),
]


@dataclass(frozen=True, slots=True)
class IntensityTrace(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(INTENSITY_TRACE_ELEMENTS)
    Values: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SpectrumBlock(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(_SPECTRUM_BLOCK_ANY_ELEMENTS)
    BlockName: str = ""
    NPoints: int = 0
    NTraces: int = 0
    WavenumberMin: float = 0.0
    WavenumberMax: float = 0.0
    FilePath: str = ""
    Wavenumbers: list[float] = field(default_factory=list)
    Intensities: list[IntensityTrace] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InstrumentSnapshot(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(INSTRUMENT_SNAPSHOT_ELEMENTS)
    Connected: bool = False
    OpusVersion: str = ""
    OpusVersionExt: str = ""
    HostIp: str = ""
    Language: str = ""


@dataclass(frozen=True, slots=True)
class DisconnectResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("State", "State", "Connection state after disconnect.", basic_dt("String"))]
    )
    State: str = ""


@dataclass(frozen=True, slots=True)
class ShowOpusResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("Shown", "Shown", "Whether the OPUS window was shown.", basic_dt("Boolean"))]
    )
    Shown: bool = False


@dataclass(frozen=True, slots=True)
class HideOpusResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("Hidden", "Hidden", "Whether the OPUS window was hidden.", basic_dt("Boolean"))]
    )
    Hidden: bool = False


# ---------------------------------------------------------------------------
# MeasurementService
# ---------------------------------------------------------------------------

MEASURE_BACKGROUND_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Resolution", "Resolution", "Spectral resolution cm-1 (OPUS RES); 0 means default.", basic_dt("Real")),
    (
        "BackgroundScans",
        "Background scans",
        "Number of background scans (OPUS NSR); 0 means default.",
        basic_dt("Integer"),
    ),
    (
        "OpticalVelocity",
        "Optical velocity",
        "OPUS VEL in kHz; 0 means omit.",
        basic_dt("Real"),
    ),
    ("Aperture", "Aperture", 'OPUS APT (e.g. "6 mm"); empty means omit.', basic_dt("String")),
    ("DetectorGain", "Detector gain", "OPUS RGN; 0 means omit.", basic_dt("Real")),
    (
        "ExperimentFile",
        "Experiment file",
        "Absolute .XPM path; empty means omit. When set, RES/NSR of 0 are omitted.",
        basic_dt("String"),
    ),
    (
        "Well",
        "Well",
        "Optional HTS-XT well label (e.g. A1). Empty means measure at current stage position.",
        basic_dt("String"),
    ),
    (
        "ExtraParameters",
        "Extra parameters",
        "Additional OPUS 3-letter codes. Void or empty object means none.",
        basic_dt("Any"),
    ),
]

MEASURE_SAMPLE_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Resolution", "Resolution", "Spectral resolution cm-1 (OPUS RES); 0 means default.", basic_dt("Real")),
    ("SampleScans", "Sample scans", "Number of sample scans (OPUS NSS); 0 means default.", basic_dt("Integer")),
    (
        "BackgroundScans",
        "Background scans",
        "Number of background scans (OPUS NSR); 0 means default.",
        basic_dt("Integer"),
    ),
    (
        "OpticalVelocity",
        "Optical velocity",
        "OPUS VEL in kHz; 0 means omit.",
        basic_dt("Real"),
    ),
    ("Aperture", "Aperture", 'OPUS APT (e.g. "6 mm"); empty means omit.', basic_dt("String")),
    ("DetectorGain", "Detector gain", "OPUS SGN; 0 means omit.", basic_dt("Real")),
    ("OutputPath", "Output path", "Absolute OPUS intermediate output path on the instrument PC.", basic_dt("String")),
    (
        "ExperimentFile",
        "Experiment file",
        "Absolute .XPM path; empty means omit. When set, RES/NSS/NSR of 0 are omitted.",
        basic_dt("String"),
    ),
    (
        "Well",
        "Well",
        "Optional HTS-XT well label (e.g. B3). Empty means measure at current stage position.",
        basic_dt("String"),
    ),
    (
        "ExtraParameters",
        "Extra parameters",
        "Additional OPUS 3-letter codes. Void or empty object means none.",
        basic_dt("Any"),
    ),
    (
        "IncludeData",
        "Include data",
        "True to include full arrays in Result.Payload.Spectra.",
        basic_dt("Boolean"),
    ),
    (
        "ExportAsm",
        "Export ASM",
        "True to write an Allotrope FTIR ASM JSON beside the OPUS file (default true).",
        basic_dt("Boolean"),
    ),
    (
        "AsmOutputPath",
        "ASM output path",
        "Explicit ASM JSON path; empty derives {opus_stem}.asm.json beside the OPUS file.",
        basic_dt("String"),
    ),
    ("SampleName", "Sample name", "OPUS SNM and ASM sample identifier.", basic_dt("String")),
    (
        "Operator",
        "Operator",
        "Experiment operator (maps to ASM analyst when not overridden).",
        basic_dt("String"),
    ),
    (
        "Project",
        "Project",
        "Experiment project name stored with the result and ASM export.",
        basic_dt("String"),
    ),
    (
        "AsmMetadata",
        "ASM metadata",
        "Optional ASM document fields. Void or empty object means none.",
        basic_dt("Any"),
    ),
]

MEASURE_SAMPLE_RESULT_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("FilePath", "File path", "Path to the saved OPUS file.", basic_dt("String")),
    ("AsmPath", "ASM path", "Path to the ASM JSON export; empty if not written.", basic_dt("String")),
    (
        "Spectra",
        "Spectra",
        "Readable spectral blocks from the OPUS file.",
        list_dt(type_id("SpectrumBlock")),
    ),
    (
        "RawResponse",
        "Raw response",
        "Raw OPUS response lines.",
        list_dt(basic_dt("String")),
    ),
]

_MEASURE_SAMPLE_RESULT_ANY_ELEMENTS: list[tuple[str, str, str, str]] = [
    *MEASURE_SAMPLE_RESULT_ELEMENTS[:2],
    (
        "Spectra",
        "Spectra",
        "Readable spectral blocks from the OPUS file.",
        list_dt(structure_dt(_SPECTRUM_BLOCK_ANY_ELEMENTS)),
    ),
    MEASURE_SAMPLE_RESULT_ELEMENTS[3],
]


@dataclass(frozen=True, slots=True)
class MeasureBackgroundRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(MEASURE_BACKGROUND_REQUEST_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"ExtraParameters"})
    Resolution: float = 0.0
    BackgroundScans: int = 0
    OpticalVelocity: float = 0.0
    Aperture: str = ""
    DetectorGain: float = 0.0
    ExperimentFile: str = ""
    Well: str = ""
    ExtraParameters: Any = None


@dataclass(frozen=True, slots=True)
class MeasureBackgroundResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("Success", "Success", "Whether OPUS reported success.", basic_dt("Boolean"))]
    )
    Success: bool = False


@dataclass(frozen=True, slots=True)
class MeasureProgress(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(MEASURE_PROGRESS_ELEMENTS)
    Percent: int = 0
    Status: str = ""


@dataclass(frozen=True, slots=True)
class MeasureSampleRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(MEASURE_SAMPLE_REQUEST_ELEMENTS)
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"ExtraParameters", "AsmMetadata"})
    Resolution: float = 0.0
    SampleScans: int = 0
    BackgroundScans: int = 0
    OpticalVelocity: float = 0.0
    Aperture: str = ""
    DetectorGain: float = 0.0
    OutputPath: str = ""
    ExperimentFile: str = ""
    Well: str = ""
    ExtraParameters: Any = None
    IncludeData: bool = False
    ExportAsm: bool = True
    AsmOutputPath: str = ""
    SampleName: str = ""
    Operator: str = ""
    Project: str = ""
    AsmMetadata: Any = None


@dataclass(frozen=True, slots=True)
class MeasureSampleResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(_MEASURE_SAMPLE_RESULT_ANY_ELEMENTS)
    FilePath: str = ""
    AsmPath: str = ""
    Spectra: list[SpectrumBlock] = field(default_factory=list)
    RawResponse: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReadSpectrumRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("FilePath", "File path", "Absolute path to an OPUS data file.", basic_dt("String")),
            (
                "IncludeData",
                "Include data",
                "True to include full arrays in Spectra.",
                basic_dt("Boolean"),
            ),
        ]
    )
    FilePath: str = ""
    IncludeData: bool = False


@dataclass(frozen=True, slots=True)
class ReadSpectrumResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            (
                "Spectra",
                "Spectra",
                "Readable spectral blocks.",
                list_dt(structure_dt(_SPECTRUM_BLOCK_ANY_ELEMENTS)),
            ),
        ]
    )
    Spectra: list[SpectrumBlock] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReadParametersRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("FilePath", "File path", "Absolute path to an OPUS data file.", basic_dt("String"))]
    )
    FilePath: str = ""


@dataclass(frozen=True, slots=True)
class ReadParametersResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            (
                "Parameters",
                "Parameters",
                "OPUS parameter blocks as a nested object.",
                basic_dt("Any"),
            ),
        ]
    )
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Parameters"})
    Parameters: Any = None


@dataclass(frozen=True, slots=True)
class ExportAsmRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("FilePath", "File path", "Absolute path to an OPUS data file.", basic_dt("String")),
            (
                "AsmOutputPath",
                "ASM output path",
                "Explicit ASM JSON path; empty derives from FilePath.",
                basic_dt("String"),
            ),
            ("SampleName", "Sample name", "ASM sample identifier.", basic_dt("String")),
            (
                "Operator",
                "Operator",
                "Experiment operator (maps to ASM analyst when not overridden).",
                basic_dt("String"),
            ),
            (
                "Project",
                "Project",
                "Experiment project name stored with the result and ASM export.",
                basic_dt("String"),
            ),
            (
                "AsmMetadata",
                "ASM metadata",
                "Optional ASM document fields. Void or empty object means none.",
                basic_dt("Any"),
            ),
        ]
    )
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"AsmMetadata"})
    FilePath: str = ""
    AsmOutputPath: str = ""
    SampleName: str = ""
    Operator: str = ""
    Project: str = ""
    AsmMetadata: Any = None


@dataclass(frozen=True, slots=True)
class ExportAsmResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("AsmPath", "ASM path", "Path to the written ASM JSON document.", basic_dt("String"))]
    )
    AsmPath: str = ""


# ---------------------------------------------------------------------------
# StageControlService
# ---------------------------------------------------------------------------

MOVE_STAGE_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Device", "Device", "Stage device id; empty means OPUS default.", basic_dt("String")),
    ("X", "X", "Absolute X coordinate.", basic_dt("Real")),
    ("Y", "Y", "Absolute Y coordinate.", basic_dt("Real")),
]

MOVE_TO_WELL_REQUEST_ELEMENTS: list[tuple[str, str, str, str]] = [
    ("Well", "Well", "Well label, e.g. A1, B3, H12.", basic_dt("String")),
    (
        "PlateFormat",
        "Plate format",
        "Plate well count (96 or 24); 0 means hts_xt.json default.",
        basic_dt("Integer"),
    ),
]


@dataclass(frozen=True, slots=True)
class DrawerResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(DRAWER_RESULT_ELEMENTS)
    Function: str = ""
    Response: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DrawerProgress(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(DRAWER_PROGRESS_ELEMENTS)
    Phase: str = ""


@dataclass(frozen=True, slots=True)
class MoveStageRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(MOVE_STAGE_REQUEST_ELEMENTS)
    Device: str = ""
    X: float = 0.0
    Y: float = 0.0


@dataclass(frozen=True, slots=True)
class MoveToWellRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(MOVE_TO_WELL_REQUEST_ELEMENTS)
    Well: str = ""
    PlateFormat: int = 0


@dataclass(frozen=True, slots=True)
class MoveStageToReferencePositionRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("Device", "Device", "Stage device id; empty means OPUS default.", basic_dt("String")),
            (
                "ReferencePosition",
                "Reference position",
                "Named reference position.",
                basic_dt("String"),
            ),
        ]
    )
    Device: str = ""
    ReferencePosition: str = ""


@dataclass(frozen=True, slots=True)
class DeviceRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("Device", "Device", "Stage device id; empty means OPUS default.", basic_dt("String"))]
    )
    Device: str = ""


@dataclass(frozen=True, slots=True)
class GetStageNameRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            (
                "ImagingDevice",
                "Imaging device",
                "Imaging device id; empty means OPUS default.",
                basic_dt("String"),
            ),
        ]
    )
    ImagingDevice: str = ""


@dataclass(frozen=True, slots=True)
class SetReferencePositionRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("Device", "Device", "Stage device id; empty means OPUS default.", basic_dt("String")),
            (
                "ReferencePosition",
                "Reference position",
                "Named reference position to define.",
                basic_dt("String"),
            ),
            ("X", "X", "Absolute X coordinate.", basic_dt("Real")),
            ("Y", "Y", "Absolute Y coordinate.", basic_dt("Real")),
            ("Z", "Z", "Absolute Z coordinate.", basic_dt("Real")),
        ]
    )
    Device: str = ""
    ReferencePosition: str = ""
    X: float = 0.0
    Y: float = 0.0
    Z: float = 0.0


# ---------------------------------------------------------------------------
# OpusFunctionService
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecuteFunctionRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("FunctionName", "Function name", "PascalCase OPUS function name.", basic_dt("String")),
            ("FileId", "File id", "OPUS file id; 0 means omit.", basic_dt("Integer")),
            ("BlockName", "Block name", "OPUS block name; empty means omit.", basic_dt("String")),
            (
                "Parameters",
                "Parameters",
                "OPUS 3-letter parameter codes. Void or empty object means none.",
                basic_dt("Any"),
            ),
        ]
    )
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"Parameters"})
    FunctionName: str = ""
    FileId: int = 0
    BlockName: str = ""
    Parameters: Any = None


@dataclass(frozen=True, slots=True)
class ExecuteFunctionResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("Function", "Function", "OPUS function that was invoked.", basic_dt("String")),
            ("Response", "Response", "Raw OPUS response lines.", list_dt(basic_dt("String"))),
        ]
    )
    Function: str = ""
    Response: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ListFunctionsResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            (
                "Names",
                "Names",
                "Available OPUS function names.",
                list_dt(basic_dt("String")),
            ),
        ]
    )
    Names: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DescribeFunctionRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("FunctionName", "Function name", "PascalCase OPUS function name.", basic_dt("String"))]
    )
    FunctionName: str = ""


@dataclass(frozen=True, slots=True)
class DescribeFunctionResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("Function", "Function", "OPUS function name.", basic_dt("String")),
            (
                "InputParameters",
                "Input parameters",
                "3-letter input parameter codes.",
                list_dt(basic_dt("String")),
            ),
            (
                "ReturnParameters",
                "Return parameters",
                "Return parameter codes.",
                list_dt(basic_dt("String")),
            ),
            (
                "ParameterDetails",
                "Parameter details",
                "code to {description, dtype, default} for inputs with metadata.",
                basic_dt("Any"),
            ),
        ]
    )
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset({"ParameterDetails"})
    Function: str = ""
    InputParameters: list[str] = field(default_factory=list)
    ReturnParameters: list[str] = field(default_factory=list)
    ParameterDetails: Any = None


@dataclass(frozen=True, slots=True)
class RawCommandRequestPayload(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [("Command", "Command", "Raw OPUS C/S interpreter command.", basic_dt("String"))]
    )
    Command: str = ""


@dataclass(frozen=True, slots=True)
class RawCommandResult(AnyPayload):
    _TYPE_XML: ClassVar[str] = structure_type_xml(
        [
            ("Success", "Success", "Whether OPUS reported success.", basic_dt("Boolean")),
            ("Response", "Response", "Raw OPUS response lines.", list_dt(basic_dt("String"))),
        ]
    )
    Success: bool = False
    Response: list[str] = field(default_factory=list)


# Aliases used by stage commands that share Device-only request shapes.
GetStagePositionRequestPayload = DeviceRequestPayload
GetNumberOfAxesRequestPayload = DeviceRequestPayload
IsReferencePlateAvailableRequestPayload = DeviceRequestPayload
