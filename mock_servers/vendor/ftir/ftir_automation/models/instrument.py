"""Dataclasses for FTIR instrument state and measurement configuration."""

from __future__ import annotations

import ntpath
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ftir_automation.models.opus_params import (
    AcquisitionModeLike,
    ApodizationLike,
    PhaseCorrectionLike,
    ZeroFillFactorLike,
    coerce_opus_value,
)


@dataclass
class MeasurementConfig:
    """Parameters passed to OPUS Measure / MeasureReference functions.

    Optional parameters default to ``None`` and are then omitted from the
    OPUS payload, so the OPUS / experiment-file (.XPM) defaults apply.

    Attributes:
        resolution:       Spectral resolution in cm⁻¹ (OPUS RES, e.g. 4.0).
                          When an experiment file is set and this is ``0`` /
                          ``None``, RES is omitted so the XPM owns the value.
        sample_scans:     Number of sample scans (OPUS NSS). Same omit rule.
        background_scans: Number of background scans (OPUS NSR). Same omit rule.
        output_path:      Absolute path for the saved OPUS file (OPUS NAM).
                          Must end in a numeric extension, e.g. ``C:\\data\\sample.0``.
        output_directory: Directory for the OPUS file when *output_path* is empty
                          (OPUS PTH). Overrides the XPM directory. Set from
                          ``FTIR_OUTPUT_DIR`` when the caller leaves both empty.
        experiment_file:  Optional path to an OPUS experiment template (.XPM).
                          Sent as EXP (file name) + XPP (directory), per OPUS convention.
                          When set, PTH defaults to the XPM directory (vendor HTS workflow).
        sample_name:      OPUS SNM (spectrum / sample name). Empty omits SNM.
        acquisition_mode: Acquisition mode (OPUS AQM), e.g.
                          :attr:`AcquisitionMode.DOUBLE_SIDED_FORWARD_BACKWARD`
                          or the wire string ``"DD"``.
        zero_fill_factor: Zero-filling factor (OPUS ZFF), e.g.
                          :attr:`ZeroFillFactor.X2` or ``"2"``.
        apodization:      Apodization function (OPUS APF), e.g.
                          :attr:`Apodization.BLACKMAN_HARRIS_3_TERM` or ``"B3"``.
        phase_correction: Phase correction mode (OPUS PHZ), e.g.
                          :attr:`PhaseCorrection.MERTZ` or ``"ML"``.
        extra:            Additional raw OPUS parameters (e.g. ``{"VEL": 10}``)
                          merged into :meth:`to_opus_params`, overriding any
                          first-class field with the same OPUS key.
    """

    resolution: Optional[float] = 4.0
    sample_scans: Optional[int] = 32
    background_scans: Optional[int] = 32
    output_path: str = ""
    output_directory: str = ""
    experiment_file: Optional[str] = None
    sample_name: str = ""
    acquisition_mode: Optional[AcquisitionModeLike] = None
    zero_fill_factor: Optional[ZeroFillFactorLike] = None
    apodization: Optional[ApodizationLike] = None
    phase_correction: Optional[PhaseCorrectionLike] = None
    extra: Optional[dict[str, Any]] = None

    def to_opus_params(self) -> dict:
        """Convert to the 3-letter parameter dict expected by opuspy_api functions."""
        params: dict = {}
        xpm_mode = bool(self.experiment_file)
        if self.resolution is not None and (not xpm_mode or self.resolution > 0):
            params["RES"] = self.resolution
        if self.sample_scans is not None and (not xpm_mode or self.sample_scans > 0):
            params["NSS"] = self.sample_scans
        if self.background_scans is not None and (not xpm_mode or self.background_scans > 0):
            params["NSR"] = self.background_scans
        if self.acquisition_mode is not None:
            params["AQM"] = str(coerce_opus_value(self.acquisition_mode))
        if self.zero_fill_factor is not None:
            params["ZFF"] = str(coerce_opus_value(self.zero_fill_factor))
        if self.apodization is not None:
            params["APF"] = str(coerce_opus_value(self.apodization))
        if self.phase_correction is not None:
            params["PHZ"] = str(coerce_opus_value(self.phase_correction))
        if self.output_path:
            # OPUS expects the output file name (NAM) and its directory (PTH)
            # as separate parameters; a full path in NAM is rejected.
            directory, filename = ntpath.split(self.output_path)
            params["NAM"] = filename
            if directory:
                params["PTH"] = directory
        elif self.output_directory:
            params["PTH"] = self.output_directory.rstrip("\\/")
        if self.experiment_file:
            # OPUS expects the experiment file name (EXP) and its directory
            # (XPP) as separate parameters. Vendor HTS Measure* also sets PTH
            # to the XPM directory when no explicit output path directory is set.
            directory, filename = ntpath.split(self.experiment_file)
            params["EXP"] = filename
            if directory:
                params["XPP"] = directory
                params.setdefault("PTH", directory)
        if self.sample_name:
            params["SNM"] = self.sample_name
        if self.extra:
            params.update(self.extra)
        return params


@dataclass
class SpectrumData:
    """Spectral data extracted from a single OPUS data block.

    Attributes:
        block_name:  OPUS block name, e.g. ``"AB"`` (absorbance), ``"Raman"``.
        wavenumbers: 1-D array of wavenumber axis values (cm⁻¹), shape (N,).
        intensities: 2-D array of spectral intensities, shape (N, traces).
                     For a single-channel spectrum this is shape (N, 1).
        parameters:  Raw block parameter dict from OPUS (NPT, FXV, LXV, RES, …).
        file_path:   Source OPUS file path.
    """

    block_name: str = ""
    wavenumbers: np.ndarray = field(default_factory=lambda: np.array([]))
    intensities: np.ndarray = field(default_factory=lambda: np.array([]))
    parameters: dict = field(default_factory=dict)
    file_path: str = ""

    @property
    def n_points(self) -> int:
        """Number of spectral data points."""
        return int(self.wavenumbers.shape[0])

    @property
    def n_traces(self) -> int:
        """Number of spectral traces (channels)."""
        if self.intensities.ndim < 2:
            return 1
        return int(self.intensities.shape[1])

    @property
    def wavenumber_range(self) -> tuple[float, float]:
        """(min, max) of the wavenumber axis."""
        if self.wavenumbers.size == 0:
            return (0.0, 0.0)
        return (float(self.wavenumbers.min()), float(self.wavenumbers.max()))


@dataclass
class MeasurementResult:
    """Outcome of a single Measure call.

    Attributes:
        success:    Whether OPUS reported the measurement as successful.
        file_path:  Path to the saved OPUS intermediate file (empty on failure).
        file_id:    OPUS internal file ID (0 if not loaded).
        spectra:    Spectral blocks read back from the file.
        asm_path:   Path to the written ASM JSON export (empty if not exported).
        parameters: Operator-facing ASM settings summary (empty when export skipped).
        raw_response: Raw response list from opuspy_api for debugging.
    """

    success: bool = False
    file_path: str = ""
    file_id: int = 0
    spectra: list[SpectrumData] = field(default_factory=list)
    asm_path: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    raw_response: list = field(default_factory=list)


@dataclass
class InstrumentSnapshot:
    """Point-in-time snapshot of the OPUS / FTIR instrument state.

    Attributes:
        connected:       Whether a working HTTP connection was established.
        opus_version:    OPUS version string, e.g. ``"9.2"``.
        opus_version_ext: Extended version string with build details.
        host_ip:         IP address of the OPUS host as reported by OPUS.
        language:        OPUS UI language code, e.g. ``"English"``.
    """

    connected: bool = False
    opus_version: str = ""
    opus_version_ext: str = ""
    host_ip: str = ""
    language: str = ""
