"""Named enums for common OPUS 3-letter parameter values.

Wire values match the Bruker OPUS macro / HTTP command codes (see OPUS-NT
Programming reference). Pass members directly to ``MeasurementConfig`` fields
or as keyword arguments to OPUS wrappers — they are coerced to the underlying
int/str before the command is sent.
"""

from __future__ import annotations

from enum import Enum, IntEnum, IntFlag
from typing import Any, Union


def coerce_opus_value(value: Any) -> Any:
    """Return the OPUS wire value for an enum member; leave other values as-is."""
    if isinstance(value, Enum):
        return value.value
    return value


def normalize_opus_params(params: dict[str, Any]) -> dict[str, Any]:
    """Coerce enum members in an OPUS parameter dict to wire values."""
    return {key: coerce_opus_value(value) for key, value in params.items()}


# ---------------------------------------------------------------------------
# Save / SaveAs (COF, OEX, ADP, YON)
# ---------------------------------------------------------------------------


class SaveAsMode(IntFlag):
    """OPUS ``COF`` — bit flags for :meth:`~FileCommands.save_as` / Save.

    Format bits (``JCAMP_DX``, ``DATA_POINT_TABLE``, ``GRAMS``, ``PIROUETTE``)
    are mutually exclusive; other bits may be OR-combined with a format bit.
    """

    SAVE_ALL_BLOCKS = 2
    """COF=2 — save all data blocks (not only the selected block)."""

    MOVE_FILE = 4
    """COF=4 — move the file to the target directory after saving."""

    REMOVE_COPIES = 16
    """COF=16 — remove other loaded copies of the file after saving."""

    JCAMP_DX = 32
    """COF=32 — save as JCAMP-DX (``.dx``) file."""

    DATA_POINT_TABLE = 64
    """COF=64 — save as x,y datapoint table (``.dpt``)."""

    REPLACE_ORIGINAL_DATA = 128
    """COF=128 — replace the original data in the file."""

    GRAMS = 256
    """COF=256 — save as Galactic GRAMS file."""

    UNLOAD_AFTER_SAVE = 512
    """COF=512 — unload the file from the OPUS workspace after saving."""

    PIROUETTE = 1024
    """COF=1024 — save as Pirouette file."""


class OverwriteMode(IntEnum):
    """OPUS ``OEX`` — behaviour when the target file already exists."""

    INCREMENT_NAME = 0
    """OEX=0 — do not overwrite; increment the file name / extension."""

    OVERWRITE = 1
    """OEX=1 — overwrite an existing file at the target path."""


class DataPointsMode(IntEnum):
    """OPUS ``ADP`` — abscissa range written for an x,y / datapoint table."""

    SELECTED_RANGE = 0
    """ADP=0 — export only the currently selected data-point range."""

    ALL_POINTS = 1
    """ADP=1 — export all data points in the spectrum."""


class OrdinateMode(IntEnum):
    """OPUS ``YON`` — columns written for an x,y / datapoint table."""

    XY_VALUES = 0
    """YON=0 — write both X (wavenumber) and Y (intensity) columns."""

    Y_ONLY = 1
    """YON=1 — write Y-values only (no abscissa column)."""


# ---------------------------------------------------------------------------
# Measurement / FT parameters (AQM, APF, PHZ, ZFF)
# ---------------------------------------------------------------------------


class AcquisitionMode(str, Enum):
    """OPUS ``AQM`` — interferogram acquisition mode."""

    SINGLE_SIDED = "SN"
    """AQM=SN — single-sided interferogram."""

    DOUBLE_SIDED = "DN"
    """AQM=DN — double-sided interferogram."""

    SINGLE_SIDED_FAST_RETURN = "SF"
    """AQM=SF — single-sided, fast return."""

    SINGLE_SIDED_FORWARD_BACKWARD = "SD"
    """AQM=SD — single-sided, forward-backward."""

    DOUBLE_SIDED_FORWARD_BACKWARD = "DD"
    """AQM=DD — double-sided, forward-backward (common default)."""

    DOUBLE_SIDED_FAST_RETURN = "DF"
    """AQM=DF — double-sided, fast return."""


class Apodization(str, Enum):
    """OPUS ``APF`` — Fourier-transform apodization function."""

    BOXCAR = "BX"
    """APF=BX — Boxcar."""

    TRIANGULAR = "TR"
    """APF=TR — Triangular."""

    FOUR_POINT = "4P"
    """APF=4P — Four-point."""

    HAPP_GENZEL = "HG"
    """APF=HG — Happ-Genzel."""

    BLACKMAN_HARRIS_3_TERM = "B3"
    """APF=B3 — Blackman-Harris 3-term."""

    BLACKMAN_HARRIS_4_TERM = "B4"
    """APF=B4 — Blackman-Harris 4-term."""

    NORTON_BEER_WEAK = "NBW"
    """APF=NBW — Norton-Beer, weak."""

    NORTON_BEER_MEDIUM = "NBM"
    """APF=NBM — Norton-Beer, medium."""

    NORTON_BEER_STRONG = "NBS"
    """APF=NBS — Norton-Beer, strong."""

    USER_ONE = "US1"
    """APF=US1 — user-defined apodization #1."""

    USER_TWO = "US2"
    """APF=US2 — user-defined apodization #2."""


class PhaseCorrection(str, Enum):
    """OPUS ``PHZ`` — phase-correction mode."""

    MERTZ = "ML"
    """PHZ=ML — Mertz."""

    MERTZ_SIGNED = "SM"
    """PHZ=SM — Mertz signed."""

    POWER_SPECTRUM = "PW"
    """PHZ=PW — power spectrum (no phase correction)."""

    MERTZ_NO_PEAK_SEARCH = "MLP"
    """PHZ=MLP — Mertz / no peak search."""

    MERTZ_SIGNED_NO_PEAK_SEARCH = "SMP"
    """PHZ=SMP — Mertz signed / no peak search."""

    POWER_NO_PEAK_SEARCH = "PWP"
    """PHZ=PWP — power spectrum / no peak search."""

    MERTZ_STORED_PHASE = "MS"
    """PHZ=MS — Mertz with stored phase."""

    NONE_COMPLEX = "NO"
    """PHZ=NO — no phase correction; keep complex data."""


class ZeroFillFactor(str, Enum):
    """OPUS ``ZFF`` — zero-filling factor (OPUS string codes)."""

    X1 = "1"
    """ZFF=1 — no extra zero-filling (factor 1)."""

    X2 = "2"
    """ZFF=2 — zero-fill factor 2."""

    X4 = "4"
    """ZFF=4 — zero-fill factor 4."""

    X8 = "8"
    """ZFF=8 — zero-fill factor 8."""

    X16 = "16"
    """ZFF=16 — zero-fill factor 16."""

    X32 = "32"
    """ZFF=32 — zero-fill factor 32."""

    X64 = "64"
    """ZFF=64 — zero-fill factor 64."""

    X128 = "128"
    """ZFF=128 — zero-fill factor 128."""

    X256 = "256"
    """ZFF=256 — zero-fill factor 256."""

    X512 = "512"
    """ZFF=512 — zero-fill factor 512."""


# Type aliases used by MeasurementConfig / callers
AcquisitionModeLike = Union[AcquisitionMode, str]
ApodizationLike = Union[Apodization, str]
PhaseCorrectionLike = Union[PhaseCorrection, str]
ZeroFillFactorLike = Union[ZeroFillFactor, str]
