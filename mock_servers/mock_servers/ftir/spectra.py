"""Synthetic absorbance spectra.

Adapted from marvel_digitalisation/ftir/scripts/simulate_asm_output.py, which
already generates exactly this. It is copied rather than imported because that
file is a script: importing it would run its own sys.path setup and argparse.
"""

from __future__ import annotations

import numpy as np

PEAK_CENTRE_CM1 = 1650.0
PEAK_WIDTH_CM1 = 30.0


def synthetic_absorbance(points: int = 1024, first: float = 400.0, last: float = 4000.0):
    """A baseline, a Gaussian absorbance peak, and a little ripple.

    Returns (wavenumbers, intensities) shaped for SpectrumData: the wavenumber
    axis is 1-D of length N, intensities are 2-D as (N, traces).
    """
    wavenumbers = np.linspace(first, last, points)
    baseline = 0.02 + 1e-5 * (wavenumbers - first)
    peak = 0.85 * np.exp(-((wavenumbers - PEAK_CENTRE_CM1) ** 2) / (2 * PEAK_WIDTH_CM1**2))
    ripple = 0.004 * np.sin(wavenumbers / 40.0)
    absorbance = baseline + peak + ripple
    return wavenumbers, absorbance.reshape(-1, 1)


def spectrum_parameters(points: int, first: float, last: float, resolution: float) -> dict:
    """The OPUS block parameter dict a real read-back would carry."""
    return {
        "NPT": points,
        "FXV": first,
        "LXV": last,
        "RES": resolution,
        "DXU": "WN",
        "CSF": 1.0,
        "MXY": 1.0,
        "MNY": 0.0,
    }
