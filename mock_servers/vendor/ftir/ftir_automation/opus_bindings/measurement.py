"""Curated bindings for OPUS measurement and reference-handling functions.

All methods are thin snake_case delegations to the generated OPUS functions.
Pass any OPUS 3-letter parameter codes as keyword arguments (e.g. ``RES=4.0,
NSS=32, NAM=r"C:\\data\\out.0"``). Use ``ftir.opus.describe("Measure")`` to
inspect the available codes for any function. ``OpusAPIError`` is raised by
opuspy_api on failure.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from ftir_automation.models.opus_params import normalize_opus_params
from ftir_automation.opus_bindings.proxy import _CommandGroup

FileId = Optional[Union[int, list]]
BlockName = Optional[Union[str, list]]


class MeasurementCommands(_CommandGroup):
    """Measurement, scanning, and reference acquisition (OPUS Measure family)."""

    def measure(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Measure — primary measurement. Common params: RES, NSS, NSR, NAM, EXP."""
        return self._fn.Measure(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def measure_reference(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MeasureReference — acquire a background/reference scan. Common params: RES, NSR, EXP."""
        return self._fn.MeasureReference(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def measure_sample(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MeasureSample — acquire a sample scan against the stored reference. Common params: RES, NSS, NAM."""
        return self._fn.MeasureSample(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def save_reference(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SaveReference — persist the current reference spectrum."""
        return self._fn.SaveReference(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def load_reference(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS LoadReference — restore a previously saved reference spectrum."""
        return self._fn.LoadReference(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def clear_reference(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ClearReference — discard the stored reference spectrum."""
        return self._fn.ClearReference(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def measure_repeated(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MeasureRepeated — repeated measurements (e.g. kinetics series)."""
        return self._fn.MeasureRepeated(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def easy_measurement(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS EasyMeasurement — simplified measurement workflow."""
        return self._fn.EasyMeasurement(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def auto_measure(self, **params: Any) -> list:
        """Run OPUS AutoMeasure — automated measurement."""
        return self._fn.AutoMeasure(**normalize_opus_params(params))

    def measure_reference_multiple(self, **params: Any) -> list:
        """Run OPUS MeasureReferenceMultiple — multi-point reference acquisition."""
        return self._fn.MeasureReferenceMultiple(**normalize_opus_params(params))

    def measure_sample_multiple(self, **params: Any) -> list:
        """Run OPUS MeasureSampleMultiple — multi-point sample acquisition."""
        return self._fn.MeasureSampleMultiple(**normalize_opus_params(params))

    def measure_multiple_experiments(self, **params: Any) -> list:
        """Run OPUS MeasureMultipleExperiments — sequence of different experiments."""
        return self._fn.MeasureMultipleExperiments(**normalize_opus_params(params))

    def grid_mapping_measurement(self, **params: Any) -> list:
        """Run OPUS GridMappingMeasurement — mapping over a rectangular grid."""
        return self._fn.GridMappingMeasurement(**normalize_opus_params(params))

    def list_mapping_measurement(self, **params: Any) -> list:
        """Run OPUS ListMappingMeasurement — mapping over an explicit position list."""
        return self._fn.ListMappingMeasurement(**normalize_opus_params(params))

    def reaction_monitoring(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ReactionMonitoring — time-series reaction monitoring."""
        return self._fn.ReactionMonitoring(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def measure_rapid_trs(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MeasureRapidTRS — rapid-scan time-resolved spectroscopy."""
        return self._fn.MeasureRapidTRS(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def measure_step_scan_trs(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MeasureStepScanTRS — step-scan time-resolved spectroscopy."""
        return self._fn.MeasureStepScanTRS(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def setup_parameter(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SetupParameter — configure measurement parameters without measuring."""
        return self._fn.SetupParameter(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def check_meas_parms(self, **params: Any) -> list:
        """Run OPUS CheckMeasParms — validate the current measurement parameters."""
        return self._fn.CheckMeasParms(**normalize_opus_params(params))
