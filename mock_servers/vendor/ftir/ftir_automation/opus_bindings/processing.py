"""Curated bindings for OPUS spectral processing and manipulation functions.

Most processing functions operate on an already loaded file: pass the
``file_id`` returned by the file loader and the target ``block_name``
(e.g. ``"AB"``). Pass OPUS 3-letter parameter codes as keyword arguments;
use ``ftir.opus.describe("Baseline")`` to inspect available codes.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from ftir_automation.opus_bindings.proxy import _CommandGroup

FileId = Optional[Union[int, list]]
BlockName = Optional[Union[str, list]]


class ProcessingCommands(_CommandGroup):
    """Spectral post-processing (baseline, smoothing, transforms, arithmetic)."""

    def baseline(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Baseline — baseline correction."""
        return self._fn.Baseline(file_id=file_id, block_name=block_name, **params)

    def smooth(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Smooth — smoothing (e.g. Savitzky-Golay). Common params: QSP."""
        return self._fn.Smooth(file_id=file_id, block_name=block_name, **params)

    def wavelet_smoothing(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS WaveletSmoothing — wavelet-based smoothing."""
        return self._fn.WaveletSmoothing(file_id=file_id, block_name=block_name, **params)

    def derivative(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Derivative — n-th order derivative. Common params: QSP, QOD."""
        return self._fn.Derivative(file_id=file_id, block_name=block_name, **params)

    def normalize(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Normalize — min-max / vector normalization."""
        return self._fn.Normalize(file_id=file_id, block_name=block_name, **params)

    def subtract(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Subtract — spectral subtraction."""
        return self._fn.Subtract(file_id=file_id, block_name=block_name, **params)

    def cut(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Cut — cut a wavenumber range."""
        return self._fn.Cut(file_id=file_id, block_name=block_name, **params)

    def calculate(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Calculate — spectral arithmetic via formula."""
        return self._fn.Calculate(file_id=file_id, block_name=block_name, **params)

    def average(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Average — average several spectra."""
        return self._fn.Average(file_id=file_id, block_name=block_name, **params)

    def merge(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Merge — merge spectral ranges/spectra."""
        return self._fn.Merge(file_id=file_id, block_name=block_name, **params)

    def integrate(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Integrate — band integration via an integration method."""
        return self._fn.Integrate(file_id=file_id, block_name=block_name, **params)

    def ab_tr(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ABTR — convert between absorbance and transmittance."""
        return self._fn.ABTR(file_id=file_id, block_name=block_name, **params)

    def convert(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Convert — spectral data type conversion."""
        return self._fn.Convert(file_id=file_id, block_name=block_name, **params)

    def make_compatible(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS MakeCompatible — interpolate spectra to a common x-axis."""
        return self._fn.MakeCompatible(file_id=file_id, block_name=block_name, **params)

    def fft(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS FFT — Fourier transform of an interferogram."""
        return self._fn.FFT(file_id=file_id, block_name=block_name, **params)

    def inverse_ft(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS InverseFT — inverse Fourier transform."""
        return self._fn.InverseFT(file_id=file_id, block_name=block_name, **params)

    def deconvolution(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Deconvolution — Fourier self-deconvolution."""
        return self._fn.Deconvolution(file_id=file_id, block_name=block_name, **params)

    def kramers_kronig(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS KramersKronig — Kramers-Kronig transformation."""
        return self._fn.KramersKronig(file_id=file_id, block_name=block_name, **params)

    def raman_correction(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS RamanCorrection — Raman intensity correction."""
        return self._fn.RamanCorrection(file_id=file_id, block_name=block_name, **params)

    def black_body(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS BlackBody — black-body radiation correction."""
        return self._fn.BlackBody(file_id=file_id, block_name=block_name, **params)

    def frequency_calibration(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS FrequencyCalibration — wavenumber axis calibration."""
        return self._fn.FrequencyCalibration(file_id=file_id, block_name=block_name, **params)

    def extended_atr(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ExtendedATR — extended ATR correction."""
        return self._fn.ExtendedATR(file_id=file_id, block_name=block_name, **params)

    def straight_line(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS StraightLine — replace a range with a straight line."""
        return self._fn.StraightLine(file_id=file_id, block_name=block_name, **params)
