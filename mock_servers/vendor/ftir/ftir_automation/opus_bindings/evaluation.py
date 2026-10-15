"""Curated bindings for OPUS evaluation, analysis, and chemometrics functions.

Pass OPUS 3-letter parameter codes as keyword arguments; use
``ftir.opus.describe("PeakPick")`` to inspect available codes.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from ftir_automation.opus_bindings.proxy import _CommandGroup

FileId = Optional[Union[int, list]]
BlockName = Optional[Union[str, list]]


class EvaluationCommands(_CommandGroup):
    """Peak picking, quality tests, library search, and chemometrics."""

    def peak_pick(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS PeakPick — automatic peak detection."""
        return self._fn.PeakPick(file_id=file_id, block_name=block_name, **params)

    def signal_to_noise(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SignalToNoise — S/N ratio calculation."""
        return self._fn.SignalToNoise(file_id=file_id, block_name=block_name, **params)

    def quality_test(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS QualityTest — spectrum quality assessment."""
        return self._fn.QualityTest(file_id=file_id, block_name=block_name, **params)

    def statistics(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Statistics — statistical analysis of spectra."""
        return self._fn.Statistics(file_id=file_id, block_name=block_name, **params)

    def quick_compare(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS QuickCompare — fast spectral comparison against references."""
        return self._fn.QuickCompare(file_id=file_id, block_name=block_name, **params)

    def library_search_spectrum(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS LibrarySearchSpectrum — full-spectrum library search."""
        return self._fn.LibrarySearchSpectrum(file_id=file_id, block_name=block_name, **params)

    def library_search_peak(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS LibrarySearchPeak — peak-based library search."""
        return self._fn.LibrarySearchPeak(file_id=file_id, block_name=block_name, **params)

    def ident_analysis(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS IdentAnalysis — identity test against an IDENT method."""
        return self._fn.IdentAnalysis(file_id=file_id, block_name=block_name, **params)

    def cluster_analysis(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ClusterAnalysis — hierarchical cluster analysis."""
        return self._fn.ClusterAnalysis(file_id=file_id, block_name=block_name, **params)

    def pca(self, **params: Any) -> list:
        """Run OPUS PCA — principal component analysis."""
        return self._fn.PCA(**params)

    def quant1(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Quant1 — single-component quantification."""
        return self._fn.Quant1(file_id=file_id, block_name=block_name, **params)

    def quant2(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Quant2 — multivariate quantification (PLS)."""
        return self._fn.Quant2(file_id=file_id, block_name=block_name, **params)
