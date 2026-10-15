"""
ftir_automation — Bruker FTIR spectrometer automation via Bruker OPUS HTTP API.

Quick start::

    from ftir_automation import FtirAutomation, MeasurementConfig

    with FtirAutomation(opus_ip="127.0.0.1", opus_port=100) as ftir:
        config = MeasurementConfig(
            resolution=4.0,
            sample_scans=32,
            background_scans=32,
            output_path=r"C:\\OPUS\\Data\\sample.0",
        )
        result = ftir.measure(config)
        for spectrum in result.spectra:
            print(spectrum.block_name, spectrum.wavenumber_range)
"""

from ftir_automation.asm import build_ftir_asm, write_ftir_asm
from ftir_automation.core import FtirAutomation, get_ftir
from ftir_automation.hts_xt import HtsXtController, HtsXtGeometry, parse_well
from ftir_automation.models import (
    AcquisitionMode,
    Apodization,
    DataPointsMode,
    InstrumentSnapshot,
    MeasurementConfig,
    MeasurementResult,
    OrdinateMode,
    OverwriteMode,
    PhaseCorrection,
    SaveAsMode,
    SpectrumData,
    ZeroFillFactor,
)
from ftir_automation.opus_bindings import (
    EvaluationCommands,
    FileCommands,
    InstrumentCommands,
    MeasurementCommands,
    OpusFunctionsProxy,
    ProcessingCommands,
)

__all__ = [
    "FtirAutomation",
    "get_ftir",
    "build_ftir_asm",
    "write_ftir_asm",
    "HtsXtController",
    "HtsXtGeometry",
    "parse_well",
    "InstrumentSnapshot",
    "MeasurementConfig",
    "MeasurementResult",
    "SpectrumData",
    "AcquisitionMode",
    "Apodization",
    "DataPointsMode",
    "OrdinateMode",
    "OverwriteMode",
    "PhaseCorrection",
    "SaveAsMode",
    "ZeroFillFactor",
    "EvaluationCommands",
    "FileCommands",
    "InstrumentCommands",
    "MeasurementCommands",
    "OpusFunctionsProxy",
    "ProcessingCommands",
]
