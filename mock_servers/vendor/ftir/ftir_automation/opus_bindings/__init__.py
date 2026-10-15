"""Categorized Python bindings over the 401 generated OPUS functions.

Curated namespaces (snake_case wrappers):
  - :class:`MeasurementCommands` — measurement and reference acquisition
  - :class:`InstrumentCommands`  — optics, stage, temperature, accessories
  - :class:`ProcessingCommands`  — spectral post-processing and transforms
  - :class:`FileCommands`        — file lifecycle, export, data blocks
  - :class:`EvaluationCommands`  — peak picking, library search, chemometrics

Dynamic passthrough:
  - :class:`OpusFunctionsProxy`  — every OPUS function by its original name,
    plus ``list_functions()`` and ``describe()`` discovery helpers
"""

from ftir_automation.opus_bindings.evaluation import EvaluationCommands
from ftir_automation.opus_bindings.file_io import FileCommands
from ftir_automation.opus_bindings.instrument import InstrumentCommands
from ftir_automation.opus_bindings.measurement import MeasurementCommands
from ftir_automation.opus_bindings.processing import ProcessingCommands
from ftir_automation.opus_bindings.proxy import OpusFunctionsProxy

__all__ = [
    "EvaluationCommands",
    "FileCommands",
    "InstrumentCommands",
    "MeasurementCommands",
    "OpusFunctionsProxy",
    "ProcessingCommands",
]
