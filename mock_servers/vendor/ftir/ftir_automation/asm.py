"""Allotrope Simple Model (ASM) export for FTIR spectra.

Builds an FTIR ASM REC/2024/12 JSON document from OPUS spectral blocks and
instrument parameters. The envelope follows the published standalone FTIR ASM
manifest; device-control vocabulary borrowed from REC/2026/06
(``apodization``, ``phase correction``, ``zero filling factor``) is included
when OPUS provides the corresponding codes.

Spectral cubes are emitted only when the corresponding OPUS blocks are present
(pragmatic subset). Use :func:`validate_ftir_asm` to list missing REC/2024/12
required keys. Strict full-schema CI uses a multi-block synthetic fixture.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from ftir_automation.models.instrument import InstrumentSnapshot, SpectrumData

log = logging.getLogger(__name__)

ASM_MANIFEST = "http://purl.allotrope.org/manifests/ftir/REC/2024/12/ftir.manifest"
ASM_SCHEMA_URL = "http://purl.allotrope.org/json-schemas/adm/ftir/REC/2024/12/ftir.schema"

# Prefer these OPUS acquisition codes when present in block parameters.
_ACQ_KEYS = (
    "RES",
    "NSS",
    "NSR",
    "AQM",
    "APF",
    "ZFF",
    "PHZ",
    "NPT",
    "FXV",
    "LXV",
    "CSF",
    "INS",
    "IST",
    "SNM",
    "CNM",
)


def _flatten_parameters(parameters: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Flatten nested OPUS block→param maps into a single name→value dict.

    Prefer known acquisition keys; include remaining present keys without
    hard-coding a fixed set (bench-specific fields vary).
    """
    if not parameters:
        return {}

    flat: dict[str, Any] = {}
    # Nested: block_name -> {code: value}
    nested = any(isinstance(v, dict) for v in parameters.values())
    if nested:
        for block_name, block_params in parameters.items():
            if not isinstance(block_params, dict):
                flat[str(block_name)] = block_params
                continue
            for key, value in block_params.items():
                # First occurrence wins for shared codes; block-prefixed for extras.
                if key not in flat:
                    flat[str(key)] = value
                else:
                    flat[f"{block_name}.{key}"] = value
    else:
        flat = {str(k): v for k, v in parameters.items()}

    ordered: dict[str, Any] = {}
    for key in _ACQ_KEYS:
        if key in flat:
            ordered[key] = flat[key]
    for key, value in flat.items():
        if key not in ordered:
            ordered[key] = value
    return ordered

DOCUMENT_REQUIRED_KEYS: tuple[str, ...] = (
    "analyst",
    "beamsplitter type",
    "detector type",
    "equipment serial number",
    "measurement identifier",
    "measurement time",
    "sample identifier",
)

MEASUREMENT_REQUIRED_KEYS: tuple[str, ...] = (
    "absorption spectrum data cube",
    "transmittance spectrum data cube",
    "reflectance spectrum data cube",
    "infrared interferogram data cube",
    "resolution",
    "number of averages",
    "optical velocity setting",
    "aperture size setting",
    "detector gain setting",
)

BACKGROUND_REQUIRED_KEYS: tuple[str, ...] = (
    "optical velocity setting",
    "infrared interferogram data cube",
    "aperture size setting",
    "detector gain setting",
    "resolution",
    "number of averages",
)

DEVICE_SETTING_KEYS: tuple[str, ...] = (
    "resolution",
    "number of averages",
    "optical velocity setting",
    "aperture size setting",
    "detector gain setting",
)

# Device settings plus optional REC/2026/06 control fields for the operator UI.
_OPERATOR_DEVICE_KEYS: tuple[str, ...] = (
    *DEVICE_SETTING_KEYS,
    "apodization",
    "phase correction",
    "zero filling factor",
)

# Optional document-level fields accepted via asm_metadata (plus required overrides).
OPTIONAL_DOCUMENT_KEYS: tuple[str, ...] = (
    "analytical method identifier",
    "assay comment",
    "assay data identifier",
    "batch identifier",
    "conformance assessment",
    "experimental data identifier",
    "material identifier",
    "michelson interferometer acquisition mode setting",
    "project",
    "reference identifier",
    "sample measurement interface type",
    "sample preparation description",
)

_METADATA_OVERRIDE_KEYS: frozenset[str] = frozenset(
    (*DOCUMENT_REQUIRED_KEYS, *OPTIONAL_DOCUMENT_KEYS)
)

# HeNe laser wavenumber (cm⁻¹) used by Bruker to convert scanner velocity
# from kHz (laser-fringe rate) to optical path velocity in cm/s:
#   VEL_cm_s = VEL_kHz * 1000 / (2 * 15798)
_HE_NE_WAVENUMBER_CM1 = 15798.0

# OPUS block name → (document_key, cube_key, dim_concept, dim_unit, measure_concept, measure_unit)
_BLOCK_CUBE_MAP: dict[str, tuple[str, str, str, str, str, str]] = {
    "AB": (
        "measurement document",
        "absorption spectrum data cube",
        "wavelength",
        "1/cm",
        "absorbance",
        "AU",
    ),
    "TR": (
        "measurement document",
        "transmittance spectrum data cube",
        "wavelength",
        "1/cm",
        "transmittance",
        "%",
    ),
    "RFL": (
        "measurement document",
        "reflectance spectrum data cube",
        "wavelength",
        "1/cm",
        "reflectance",
        "(unitless)",
    ),
    "Refl": (
        "measurement document",
        "reflectance spectrum data cube",
        "wavelength",
        "1/cm",
        "reflectance",
        "(unitless)",
    ),
    "IgSm": (
        "measurement document",
        "infrared interferogram data cube",
        "length",
        "cm",
        "relative intensity",
        "(unitless)",
    ),
    "IgRf": (
        "background document",
        "infrared interferogram data cube",
        "length",
        "cm",
        "relative intensity",
        "(unitless)",
    ),
}


def _qty(value: float, unit: str) -> dict[str, Any]:
    return {"value": float(value), "unit": unit}


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
        if match:
            return float(match.group(0))
    return None


def _parse_aperture_nm(value: Any) -> Optional[float]:
    """Parse OPUS APT (e.g. ``\"6 mm\"``, ``\"100 um\"``) into nanometres.

    Returns ``None`` for unparseable values such as ``\"open\"``.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"open", "n/a", "na", "-"}:
        return None
    number = _to_float(text)
    if number is None:
        return None
    if "um" in text or "µm" in text or "μm" in text:
        return number * 1_000.0
    if "nm" in text:
        return number
    if "cm" in text:
        return number * 10_000_000.0
    # Default OPUS aperture strings are millimetres (e.g. "6 mm").
    if "mm" in text or re.search(r"\d", text):
        return number * 1_000_000.0
    return None


def _vel_khz_to_cm_s(value: Any) -> Optional[float]:
    """Convert OPUS VEL (kHz laser-fringe rate) to optical velocity in cm/s."""
    khz = _to_float(value)
    if khz is None:
        return None
    return khz * 1000.0 / (2.0 * _HE_NE_WAVENUMBER_CM1)


def _first_trace(intensities: np.ndarray) -> list[float]:
    arr = np.asarray(intensities)
    if arr.ndim == 1:
        return [float(x) for x in arr.tolist()]
    if arr.size == 0:
        return []
    return [float(x) for x in arr[:, 0].tolist()]


def _datacube(
    *,
    label: str,
    dim_concept: str,
    dim_unit: str,
    measure_concept: str,
    measure_unit: str,
    dimensions: list[float],
    measures: list[float],
) -> dict[str, Any]:
    return {
        "label": label,
        "cube-structure": {
            "dimensions": [
                {
                    "@componentDatatype": "double",
                    "concept": dim_concept,
                    "unit": dim_unit,
                }
            ],
            "measures": [
                {
                    "@componentDatatype": "double",
                    "concept": measure_concept,
                    "unit": measure_unit,
                }
            ],
        },
        "data": {
            "dimensions": [dimensions],
            "measures": [measures],
        },
    }


def _parse_measurement_time(flat: dict[str, Any], override: str = "") -> str:
    if override:
        return override
    date_raw = flat.get("DAT")
    time_raw = flat.get("TIM")
    if date_raw and time_raw:
        combined = f"{date_raw} {time_raw}".strip()
        for fmt in (
            "%d/%m/%Y %H:%M:%S.%f",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(combined, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                continue
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _device_settings(
    flat: dict[str, Any],
    *,
    averages_key: str,
    gain_keys: tuple[str, ...],
    fill_defaults: bool = True,
) -> dict[str, Any]:
    """Build device-control fields shared by measurement / background documents.

    When *fill_defaults* is true, all REC/2024/12 required device-setting keys
    are present (unknown values become ``0``).
    """
    out: dict[str, Any] = {}

    res = _to_float(flat.get("RES"))
    if res is not None:
        out["resolution"] = _qty(res, "1/cm")
    elif fill_defaults:
        out["resolution"] = _qty(0.0, "1/cm")

    averages = _to_float(flat.get(averages_key))
    if averages is not None:
        out["number of averages"] = _qty(averages, "(unitless)")
    elif fill_defaults:
        out["number of averages"] = _qty(0.0, "(unitless)")

    gain_found = False
    for key in gain_keys:
        gain = _to_float(flat.get(key))
        if gain is not None:
            out["detector gain setting"] = _qty(gain, "(unitless)")
            gain_found = True
            break
    if not gain_found and fill_defaults:
        out["detector gain setting"] = _qty(0.0, "(unitless)")

    aperture_nm = _parse_aperture_nm(flat.get("APT"))
    if aperture_nm is not None:
        out["aperture size setting"] = _qty(aperture_nm, "nm")
    elif fill_defaults:
        out["aperture size setting"] = _qty(0.0, "nm")

    vel = _vel_khz_to_cm_s(flat.get("VEL"))
    if vel is not None:
        out["optical velocity setting"] = _qty(vel, "cm/s")
    elif fill_defaults:
        out["optical velocity setting"] = _qty(0.0, "cm/s")

    if flat.get("APF") is not None:
        out["apodization"] = str(flat["APF"])
    if flat.get("PHZ") is not None:
        out["phase correction"] = str(flat["PHZ"])
    zff = _to_float(flat.get("ZFF"))
    if zff is not None:
        out["zero filling factor"] = _qty(zff, "(unitless)")

    return out


def _apply_asm_metadata(meta: dict[str, Any], asm_metadata: Optional[dict[str, Any]]) -> None:
    """Merge caller-supplied ASM document fields into *meta* (in place)."""
    if not asm_metadata:
        return
    for key, value in asm_metadata.items():
        if key not in _METADATA_OVERRIDE_KEYS:
            log.debug("Ignoring unknown asm_metadata key %r", key)
            continue
        if value is None:
            continue
        if key == "conformance assessment":
            meta[key] = bool(value)
        else:
            meta[key] = value if isinstance(value, (bool, int, float)) else str(value)


def enrich_asm_metadata(
    asm_metadata: Optional[dict[str, Any]] = None,
    *,
    operator: str = "",
    project: str = "",
) -> Optional[dict[str, Any]]:
    """Fold SiLA Operator/Project into ASM metadata (analyst / project keys).

    ``AsmMetadata["analyst"]`` wins over *operator* when both are set.
    """
    meta: dict[str, Any] = dict(asm_metadata or {})
    if operator and "analyst" not in meta:
        meta["analyst"] = operator
    if project:
        meta["project"] = project
    return meta or None


def summarize_operator_parameters(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract operator-facing settings from an ASM document (no spectrum cubes).

    Returns a compact dict shaped for the dashboard Measurement result panel::

        {
            "measurement": {<device setting keys>},
            "background": {<device setting keys>},
            "document": {<required + present optional metadata>},
        }

    Quantity fields keep their ASM ``{"value", "unit"}`` objects so the UI
    matches the file on disk (velocity in cm/s, aperture in nm).
    """
    ftir = doc.get("Fourier transform infrared document")
    if not isinstance(ftir, dict):
        ftir = {}

    def _settings(subdoc: Any) -> dict[str, Any]:
        if not isinstance(subdoc, dict):
            return {}
        return {key: subdoc[key] for key in _OPERATOR_DEVICE_KEYS if key in subdoc}

    document = {
        key: doc[key]
        for key in (*DOCUMENT_REQUIRED_KEYS, *OPTIONAL_DOCUMENT_KEYS)
        if key in doc
    }

    return {
        "measurement": _settings(ftir.get("measurement document")),
        "background": _settings(ftir.get("background document")),
        "document": document,
    }


def validate_ftir_asm(doc: dict[str, Any]) -> list[str]:
    """Return missing REC/2024/12 required keys for *doc*.

    Paths use dotted notation, e.g.
    ``Fourier transform infrared document.measurement document.resolution``.
    """
    missing: list[str] = []
    for key in DOCUMENT_REQUIRED_KEYS:
        if key not in doc:
            missing.append(key)

    ftir = doc.get("Fourier transform infrared document")
    if not isinstance(ftir, dict):
        missing.append("Fourier transform infrared document")
        return missing

    meas = ftir.get("measurement document")
    if isinstance(meas, dict):
        for key in MEASUREMENT_REQUIRED_KEYS:
            if key not in meas:
                missing.append(f"Fourier transform infrared document.measurement document.{key}")
    bg = ftir.get("background document")
    if isinstance(bg, dict):
        for key in BACKGROUND_REQUIRED_KEYS:
            if key not in bg:
                missing.append(f"Fourier transform infrared document.background document.{key}")
    return missing


def build_ftir_asm(
    spectra: list[SpectrumData],
    snapshot: InstrumentSnapshot,
    sample_name: str = "",
    parameters: Optional[dict[str, Any]] = None,
    measurement_id: str = "",
    measurement_time: str = "",
    asm_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build an FTIR ASM REC/2024/12 document from OPUS spectra and parameters.

    Args:
        spectra: Readable OPUS spectral blocks.
        snapshot: Instrument / OPUS connection snapshot.
        sample_name: Optional sample name (overrides OPUS SNM).
        parameters: Optional OPUS parameter map (flat or block→dict).
        measurement_id: Optional measurement identifier; a UUID is generated
            when empty.
        measurement_time: Optional ISO-8601 timestamp; derived from OPUS DAT/TIM
            or current UTC when empty.
        asm_metadata: Optional ASM document-level field overrides / optional keys.

    Returns:
        ASM document as a plain JSON-serialisable dict (no numpy types).
    """
    flat = _flatten_parameters(parameters)
    if not flat and spectra:
        merged: dict[str, Any] = {}
        for s in spectra:
            if s.parameters:
                merged[s.block_name] = s.parameters
        flat = _flatten_parameters(merged)

    measurement_doc: dict[str, Any] = _device_settings(
        flat, averages_key="NSS", gain_keys=("SGN", "ASG"), fill_defaults=True
    )
    background_doc: dict[str, Any] = _device_settings(
        flat, averages_key="NSR", gain_keys=("RGN", "ARG"), fill_defaults=True
    )

    has_measurement_cube = False
    has_background_cube = False

    for spectrum in spectra:
        block = spectrum.block_name or ""
        mapping = _BLOCK_CUBE_MAP.get(block)
        if mapping is None:
            log.debug("Skipping OPUS block %r — no ASM cube slot", block)
            continue
        doc_key, cube_key, dim_c, dim_u, meas_c, meas_u = mapping
        dims = [float(x) for x in np.asarray(spectrum.wavenumbers).tolist()]
        measures = _first_trace(spectrum.intensities)
        cube = _datacube(
            label=(
                f"{block} interferogram"
                if block.startswith("Ig")
                else f"{block} spectrum"
            ),
            dim_concept=dim_c,
            dim_unit=dim_u,
            measure_concept=meas_c,
            measure_unit=meas_u,
            dimensions=dims,
            measures=measures,
        )
        if doc_key == "measurement document":
            measurement_doc[cube_key] = cube
            has_measurement_cube = True
        else:
            background_doc[cube_key] = cube
            has_background_cube = True

    # Include sub-documents when they carry cubes, or when OPUS supplied
    # background-specific params (otherwise omit empty background).
    has_background_params = any(k in flat for k in ("NSR", "RGN", "ARG"))
    ftir_doc: dict[str, Any] = {}
    if has_measurement_cube or spectra:
        ftir_doc["measurement document"] = measurement_doc
    if has_background_cube or has_background_params:
        ftir_doc["background document"] = background_doc

    sample_id = sample_name or (str(flat["SNM"]) if flat.get("SNM") else "FTIR sample")
    serial = flat.get("INS") or snapshot.opus_version_ext or snapshot.opus_version or "unknown"
    meta: dict[str, Any] = {
        "sample identifier": sample_id,
        "measurement identifier": measurement_id or str(uuid.uuid4()),
        "measurement time": _parse_measurement_time(flat, measurement_time),
        "analyst": str(flat["CNM"]) if flat.get("CNM") is not None else "unknown",
        "detector type": str(flat["DTC"]) if flat.get("DTC") is not None else "unknown",
        "beamsplitter type": str(flat["BMS"]) if flat.get("BMS") is not None else "unknown",
        "equipment serial number": str(serial),
    }
    if flat.get("AQM") is not None:
        meta["michelson interferometer acquisition mode setting"] = str(flat["AQM"])

    _apply_asm_metadata(meta, asm_metadata)

    missing = validate_ftir_asm(
        {
            "$asm.manifest": ASM_MANIFEST,
            "Fourier transform infrared document": ftir_doc,
            **meta,
        }
    )
    if missing:
        log.debug("ASM document missing 2024/12 required keys: %s", missing)

    return {
        "$asm.manifest": ASM_MANIFEST,
        "Fourier transform infrared document": ftir_doc,
        **meta,
    }


def write_ftir_asm(
    path: str,
    spectra: list[SpectrumData],
    snapshot: InstrumentSnapshot,
    sample_name: str = "",
    parameters: Optional[dict[str, Any]] = None,
    measurement_id: str = "",
    measurement_time: str = "",
    asm_metadata: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """Build and write an FTIR ASM JSON document to *path*.

    Returns ``(absolute path written, operator parameter summary)``.
    The summary is the same shape as :func:`summarize_operator_parameters`.
    """
    doc = build_ftir_asm(
        spectra=spectra,
        snapshot=snapshot,
        sample_name=sample_name,
        parameters=parameters,
        measurement_id=measurement_id,
        measurement_time=measurement_time,
        asm_metadata=asm_metadata,
    )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path, summarize_operator_parameters(doc)
