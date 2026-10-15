"""FTIR peak picking and polymer_dev storage after MeasureSample.

Algorithm matches the reference ``FTIR peaks (1).py`` script: transmittance
first, descending wavenumber sort, Savitzky-Golay smooth, six-bin max
quadratic baseline, low-prominence peaks for 0–1 scale spectra, intensity and
shape labels, then databank assignment.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

_log = logging.getLogger(__name__)

_SAVGOL_WINDOW = 11
_SAVGOL_POLY = 2
_N_SEGMENTS = 6
# Tuned for 0–1 transmittance-scale spectra (FTIR peaks (1).py).
_PROMINENCE = 0.001
_WIDTH_REL_HEIGHT = 0.3
_WIDTH_BROAD = 40.0
_WIDTH_SHARP = 16.0
_INTENSITY_STRONG = 0.008
_INTENSITY_MEDIUM = 0.004

# Transmittance-first to match the ASM transmittance cube in the reference script.
_BLOCK_PREFERENCE = ("TR", "AB", "RFL", "Refl")
_SKIP_BLOCKS = frozenset({"ScSm", "ScRf", "IgSm", "IgRf"})


@dataclass(frozen=True)
class Peak:
    """One detected peak after baseline correction."""

    wavenumber: float
    height: float
    transmittance: float
    intensity: str
    shape: str


@dataclass
class PeakPickResult:
    """Baseline-corrected curve and picked peaks for one spectral block."""

    block_name: str
    wavenumbers: np.ndarray
    transmittance: np.ndarray
    corrected: np.ndarray
    peaks: list[Peak] = field(default_factory=list)


def select_spectrum_block(spectra: Sequence[Any]) -> Any | None:
    """Pick the OPUS block used for peak picking (transmittance first)."""
    by_name = {
        str(getattr(s, "block_name", "") or ""): s
        for s in spectra or []
        if str(getattr(s, "block_name", "") or "")
    }
    if not by_name:
        return None
    for preferred in _BLOCK_PREFERENCE:
        if preferred in by_name:
            return by_name[preferred]
    for name, spectrum in by_name.items():
        if name not in _SKIP_BLOCKS:
            return spectrum
    return next(iter(by_name.values()))


def _first_trace(intensities: Any) -> np.ndarray:
    arr = np.asarray(intensities, dtype=float)
    if arr.ndim == 0:
        return np.array([float(arr)])
    if arr.ndim == 1:
        return arr
    return arr[:, 0]


def _clean_xy(
    wavenumbers: Any, intensities: Any
) -> tuple[np.ndarray, np.ndarray] | None:
    x = np.asarray(wavenumbers, dtype=float).ravel()
    y = _first_trace(intensities)
    if x.size == 0 or y.size == 0 or x.size != y.size:
        return None
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    if x.size == 0:
        return None
    # Standard FTIR analysis/plot order: high wavenumber on the left.
    order = np.argsort(x)[::-1]
    return x[order], y[order]


def _intensity_label(height: float) -> str:
    if height > _INTENSITY_STRONG:
        return "strong"
    if height > _INTENSITY_MEDIUM:
        return "medium"
    return "weak"


def _shape_label(width: float) -> str:
    if width > _WIDTH_BROAD:
        return "broad"
    if width < _WIDTH_SHARP:
        return "sharp"
    return "-"


def pick_peaks(
    wavenumbers: Any,
    intensities: Any,
    *,
    block_name: str = "",
) -> PeakPickResult | None:
    """Smooth, baseline-correct, and pick peaks from one spectrum trace.

    Returns ``None`` when the series is too short for Savitzky-Golay, has
    fewer than three baseline anchors, or is empty after cleaning.
    """
    from scipy.signal import find_peaks, peak_widths, savgol_filter

    cleaned = _clean_xy(wavenumbers, intensities)
    if cleaned is None:
        return None
    x, y = cleaned
    if x.size < _SAVGOL_WINDOW:
        _log.info(
            "peak pick skipped: need >= %d points, got %d",
            _SAVGOL_WINDOW,
            x.size,
        )
        return None

    y_smooth = savgol_filter(y, _SAVGOL_WINDOW, _SAVGOL_POLY)

    # Six equal-width bins on the wavenumber axis; anchor = raw max in bin.
    edges = np.linspace(float(x.min()), float(x.max()), _N_SEGMENTS + 1)
    # digitize returns 1..n for in-range; 0 and n+1 are out of range.
    segment = np.digitize(x, edges[1:-1], right=False)
    anchors_x: list[float] = []
    anchors_y: list[float] = []
    for seg in range(_N_SEGMENTS):
        idx = np.where(segment == seg)[0]
        if idx.size == 0:
            continue
        local = idx[int(np.argmax(y[idx]))]
        anchors_x.append(float(x[local]))
        anchors_y.append(float(y[local]))
    if len(anchors_x) < 3:
        _log.info(
            "peak pick skipped: need >= 3 baseline anchors, got %d",
            len(anchors_x),
        )
        return None

    coeffs = np.polyfit(np.asarray(anchors_x), np.asarray(anchors_y), deg=2)
    baseline = np.polyval(coeffs, x)
    corrected = baseline - y_smooth

    peak_idx, _ = find_peaks(corrected, prominence=_PROMINENCE)
    widths = np.zeros(len(peak_idx), dtype=float)
    if peak_idx.size:
        widths, _, _, _ = peak_widths(
            corrected, peak_idx, rel_height=_WIDTH_REL_HEIGHT
        )

    peaks: list[Peak] = []
    for i, idx in enumerate(peak_idx):
        height = float(corrected[idx])
        peaks.append(
            Peak(
                wavenumber=float(x[idx]),
                height=height,
                transmittance=float(y[idx]),
                intensity=_intensity_label(height),
                shape=_shape_label(float(widths[i])),
            )
        )

    return PeakPickResult(
        block_name=block_name,
        wavenumbers=x,
        transmittance=y,
        corrected=corrected,
        peaks=peaks,
    )


def pick_peaks_from_spectra(spectra: Sequence[Any]) -> PeakPickResult | None:
    """Select the preferred block from *spectra* and pick peaks."""
    spectrum = select_spectrum_block(spectra)
    if spectrum is None:
        _log.info("peak pick skipped: no usable spectral block")
        return None
    return pick_peaks(
        getattr(spectrum, "wavenumbers", []),
        getattr(spectrum, "intensities", None),
        block_name=str(getattr(spectrum, "block_name", "") or ""),
    )


def processed_json_path(file_path: str = "", output_dir: str = "") -> str:
    """Derive ``{stem}_processed.json`` next to the OPUS file or in *output_dir*."""
    if file_path:
        directory = os.path.dirname(file_path)
        stem = os.path.splitext(os.path.basename(file_path))[0]
        if stem:
            return os.path.join(directory or ".", f"{stem}_processed.json")
    if output_dir:
        return os.path.join(output_dir.rstrip("\\/"), "spectrum_processed.json")
    return ""


def write_processed_json(path: str, result: PeakPickResult) -> str:
    """Write transmittance curve, corrected curve, and peaks to *path*."""
    if not path:
        return ""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "block": result.block_name,
        "wavenumber": [float(v) for v in result.wavenumbers.tolist()],
        "transmittance": [float(v) for v in result.transmittance.tolist()],
        "corrected": [float(v) for v in result.corrected.tolist()],
        "peaks": [
            {
                "wavenumber": p.wavenumber,
                "intensity": p.intensity,
                "shape": p.shape,
                "height": p.height,
                "transmittance": p.transmittance,
            }
            for p in result.peaks
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return path


def store_peaks(
    result: PeakPickResult,
    *,
    sample_id: str,
    run_id: str,
    operator: str = "",
    project: str = "",
    database_url: str | None = None,
) -> int:
    """Insert peaks into polymer_dev tables. Returns the number of peaks stored.

    *operator* and *project* are the MeasureSample command inputs and are
    written onto the ``polymer_dev.runs`` row. Blank values are stored as NULL
    so a later write can fill them.

    All statements are schema-qualified ``polymer_dev.*``. Missing URL, missing
    driver, or empty *sample_id* / *run_id* skips the write.
    """
    sample_id = (sample_id or "").strip()
    run_id = (run_id or "").strip()
    if not sample_id or not run_id:
        _log.info("peak store skipped: sample_id or run_id empty")
        return 0
    dsn = (database_url or os.environ.get("LAB_DATABASE_URL") or "").strip()
    if not dsn:
        _log.info("peak store skipped: LAB_DATABASE_URL unset")
        return 0
    try:
        import psycopg
    except ImportError:
        _log.info("peak store skipped: psycopg not installed")
        return 0

    peaks = result.peaks
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO polymer_dev.samples (sampleid, samplename)
                    VALUES (%(sampleid)s, %(samplename)s)
                    ON CONFLICT (sampleid) DO NOTHING
                    """,
                    {"sampleid": sample_id, "samplename": sample_id},
                )
                recorded_at = datetime.now(timezone.utc)
                cur.execute(
                    """
                    INSERT INTO polymer_dev.runs (
                        run_id, instrument_id, instrument_kind, sample_id,
                        operator, project, started_at, finished_at, outcome
                    ) VALUES (
                        %(run_id)s, %(instrument_id)s, %(instrument_kind)s,
                        %(sample_id)s, %(operator)s, %(project)s,
                        %(started_at)s, %(finished_at)s, %(outcome)s
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        finished_at = COALESCE(runs.finished_at, EXCLUDED.finished_at),
                        outcome = COALESCE(runs.outcome, EXCLUDED.outcome),
                        operator = COALESCE(EXCLUDED.operator, runs.operator),
                        project = COALESCE(EXCLUDED.project, runs.project)
                    """,
                    {
                        "run_id": run_id,
                        "instrument_id": (
                            os.environ.get("LAB_INSTRUMENT_ID") or "ftir-01"
                        ).strip()
                        or "ftir-01",
                        "instrument_kind": "ftir",
                        "sample_id": sample_id,
                        "operator": (operator or "").strip() or None,
                        "project": (project or "").strip() or None,
                        "started_at": recorded_at,
                        "finished_at": recorded_at,
                        "outcome": "ok",
                    },
                )
                if peaks:
                    cur.executemany(
                        """
                        INSERT INTO polymer_dev.ftirpeaks (
                            sampleid, runid, peakid, wavenumber, intensity, shape
                        ) VALUES (
                            %(sampleid)s, %(runid)s, %(peakid)s,
                            %(wavenumber)s, %(intensity)s, %(shape)s
                        )
                        ON CONFLICT (runid, peakid) DO NOTHING
                        """,
                        [
                            {
                                "sampleid": sample_id,
                                "runid": run_id,
                                "peakid": i,
                                "wavenumber": round(p.wavenumber, 2),
                                "intensity": p.intensity,
                                "shape": p.shape,
                            }
                            for i, p in enumerate(peaks, start=1)
                        ],
                    )
                    cur.execute(
                        """
                        INSERT INTO polymer_dev.peak_assignment (
                            runid, peakid, functional_group, class, match_score
                        )
                        SELECT
                            f.runid,
                            f.peakid,
                            d.functional_group,
                            d.class,
                            (
                                CASE WHEN f.intensity = d.intensity THEN 1 ELSE 0 END
                                + CASE WHEN f.shape = d.shape THEN 1 ELSE 0 END
                            )::numeric AS match_score
                        FROM polymer_dev.ftirpeaks AS f
                        INNER JOIN polymer_dev.ftirdatabank AS d
                          ON f.wavenumber BETWEEN d.wavenumber_min AND d.wavenumber_max
                        WHERE f.runid = %(runid)s
                        ON CONFLICT (runid, peakid, functional_group) DO NOTHING
                        """,
                        {"runid": run_id},
                    )
    return len(peaks)
