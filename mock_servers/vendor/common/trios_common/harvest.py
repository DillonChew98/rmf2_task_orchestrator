"""Normalize TRIOS export-json files into curve.json and mirror to the dataplane.

Designed to run inside the instrument SiLA server process (files are local).
Failures are logged and never raised into the SiLA call path.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_log = logging.getLogger(__name__)

# Default column → canonical signal matching (overridden by manifest curves.signals).
_DEFAULT_MATCH: dict[str, list[str]] = {
    "time": ["Time", "time"],
    "temperature": ["Temperature", "temperature"],
    "heat_flow": ["Heat Flow", "HeatFlow", "heat_flow"],
    "weight": ["Weight", "weight"],
    "weight_pct": ["Weight (%)", "Weight%", "weight_pct"],
}

_DEFAULT_UNITS: dict[str, str] = {
    "time": "s",
    "temperature": "degC",
    "heat_flow": "mW",
    "weight": "mg",
    "weight_pct": "%",
}

_CHANNEL_SUFFIX = re.compile(r"_([A-Za-z0-9]+)\.(?:json|gz)$", re.IGNORECASE)


@dataclass(frozen=True)
class CurveSignalSpec:
    unit: str = ""
    match: list[str] = field(default_factory=list)


@dataclass
class NormalizedCurve:
    channel: str
    sample_id: str
    signals: dict[str, dict[str, Any]]  # name → {unit, values}


def load_export_json(path: str | Path) -> Any:
    """Load a ``.json`` or ``.json.gz`` TRIOS export."""
    p = Path(path)
    if p.suffix == ".gz" or p.name.endswith(".json.gz"):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _variables_from_doc(doc: Any) -> dict[str, tuple[str, list[float]]]:
    """Return ``{name: (unit, values)}`` from supported export shapes."""
    out: dict[str, tuple[str, list[float]]] = {}
    if not isinstance(doc, dict):
        return out

    variables = doc.get("variables")
    if isinstance(variables, list):
        for item in variables:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("Name") or "")
            if not name:
                continue
            unit = str(item.get("unit") or item.get("Unit") or "")
            values = item.get("values") or item.get("Values") or []
            try:
                out[name] = (unit, [float(v) for v in values])
            except (TypeError, ValueError):
                continue
        return out

    columns = doc.get("columns") or doc.get("Columns")
    if isinstance(columns, dict):
        for name, values in columns.items():
            try:
                out[str(name)] = ("", [float(v) for v in values])
            except (TypeError, ValueError):
                continue
        return out

    # Flat dict of arrays
    for name, values in doc.items():
        if isinstance(values, list) and values and isinstance(values[0], (int, float)):
            try:
                out[str(name)] = ("", [float(v) for v in values])
            except (TypeError, ValueError):
                continue
    return out


def _match_signal(
    column_name: str, specs: Mapping[str, CurveSignalSpec]
) -> str | None:
    for canonical, spec in specs.items():
        for candidate in spec.match:
            if column_name.lower() == candidate.lower():
                return canonical
    return None


def infer_channel(path: str | Path, doc: Any) -> str:
    if isinstance(doc, dict):
        ch = doc.get("channel") or doc.get("Channel") or doc.get("sensor")
        if ch:
            return str(ch)
    m = _CHANNEL_SUFFIX.search(Path(path).name)
    if m:
        return m.group(1).upper()
    return "A"


def normalize_export(
    doc: Any,
    *,
    path: str | Path = "",
    signal_specs: Mapping[str, CurveSignalSpec] | None = None,
) -> NormalizedCurve:
    """Map a TRIOS export document to canonical curve signals."""
    specs = dict(signal_specs or {})
    if not specs:
        specs = {
            k: CurveSignalSpec(unit=_DEFAULT_UNITS.get(k, ""), match=list(v))
            for k, v in _DEFAULT_MATCH.items()
        }

    columns = _variables_from_doc(doc)
    signals: dict[str, dict[str, Any]] = {}
    for col_name, (unit, values) in columns.items():
        canonical = _match_signal(col_name, specs)
        if not canonical:
            continue
        spec = specs[canonical]
        signals[canonical] = {
            "unit": spec.unit or unit or _DEFAULT_UNITS.get(canonical, ""),
            "values": values,
        }

    sample_id = ""
    if isinstance(doc, dict):
        sample_id = str(
            doc.get("sampleName")
            or doc.get("SampleName")
            or doc.get("sample_id")
            or ""
        )

    return NormalizedCurve(
        channel=infer_channel(path, doc),
        sample_id=sample_id,
        signals=signals,
    )


def curve_to_json_dict(
    curve: NormalizedCurve,
    *,
    run_id: str,
    instrument_kind: str,
) -> dict[str, Any]:
    return {
        "event": "thermal.curve",
        "run_id": run_id,
        "instrument_kind": instrument_kind,
        "channel": curve.channel,
        "sample_id": curve.sample_id,
        "signals": curve.signals,
    }


def downsample_signal(values: list[float], max_points: int) -> list[float]:
    if max_points <= 0 or len(values) <= max_points:
        return values
    step = len(values) / max_points
    return [values[int(i * step)] for i in range(max_points)]


def harvest_export_paths(
    output_paths: list[str],
    *,
    run_id: str,
    instrument_id: str,
    instrument_kind: str,
    sample_id: str = "",
    signal_specs: Mapping[str, CurveSignalSpec] | None = None,
    mirror: bool = True,
) -> list[NormalizedCurve]:
    """Normalize each export path and optionally mirror to the dataplane.

    Never raises.
    """
    curves: list[NormalizedCurve] = []
    artifacts: list[Any] = []

    for path in output_paths:
        try:
            if not path or not os.path.isfile(path):
                _log.warning("harvest: missing export file %s", path)
                continue
            doc = load_export_json(path)
            curve = normalize_export(doc, path=path, signal_specs=signal_specs)
            if sample_id and not curve.sample_id:
                curve.sample_id = sample_id
            curves.append(curve)

            # Write curve.json next to the export (or in temp).
            curve_doc = curve_to_json_dict(
                curve, run_id=run_id, instrument_kind=instrument_kind
            )
            curve_name = f"curve_{curve.channel}.json"
            curve_path = str(Path(path).with_name(curve_name))
            try:
                Path(curve_path).write_text(
                    json.dumps(curve_doc), encoding="utf-8"
                )
            except OSError:
                tmp = tempfile.NamedTemporaryFile(
                    prefix="curve_", suffix=".json", delete=False
                )
                tmp.write(json.dumps(curve_doc).encode("utf-8"))
                tmp.close()
                curve_path = tmp.name

            if mirror:
                try:
                    from lab_observability.dataplane import Artifact

                    artifacts.append(
                        Artifact(local_path=path, kind="export_json")
                    )
                    artifacts.append(
                        Artifact(
                            local_path=curve_path,
                            kind="curve",
                            media_type="application/json",
                        )
                    )
                    # Sibling .tri if present
                    tri = Path(path)
                    for cand in (
                        tri.with_suffix(".tri"),
                        tri.with_name(tri.name.replace(".json.gz", ".tri").replace(".json", ".tri")),
                    ):
                        if cand.is_file():
                            artifacts.append(
                                Artifact(local_path=str(cand), kind="tri")
                            )
                            break
                except ImportError:
                    _log.debug("lab_observability not installed; skip mirror")
        except Exception:
            _log.exception("harvest: failed for %s", path)

    if mirror and artifacts:
        try:
            from lab_observability.dataplane import DatasetRecord, mirror_dataset

            mirror_dataset(
                DatasetRecord(
                    run_id=run_id,
                    instrument_id=instrument_id,
                    instrument_kind=instrument_kind,
                    sample_id=sample_id or (curves[0].sample_id if curves else ""),
                    artifacts=artifacts,
                    write_datasets_row=False,
                )
            )
        except Exception:
            _log.exception("harvest: mirror_dataset failed")

    return curves


def extract_output_paths(job_payload: Any) -> list[str]:
    """Pull ``outputPaths`` from a GetExportJsonJob result body."""
    paths: list[str] = []
    if not isinstance(job_payload, dict):
        return paths
    outputs = (
        job_payload.get("outputs")
        or job_payload.get("Outputs")
        or dig_body(job_payload)
    )
    if isinstance(outputs, dict):
        outputs = outputs.get("outputs") or outputs.get("Outputs") or []
    if not isinstance(outputs, list):
        return paths
    for item in outputs:
        if not isinstance(item, dict):
            continue
        for key in ("outputPaths", "OutputPaths", "output_paths"):
            vals = item.get(key)
            if isinstance(vals, list):
                paths.extend(str(v) for v in vals if v)
            elif isinstance(vals, str) and vals:
                paths.append(vals)
        single = item.get("outputPath") or item.get("OutputPath")
        if single:
            paths.append(str(single))
    return paths


def dig_body(payload: Any) -> Any:
    if isinstance(payload, dict) and "Body" in payload:
        return payload["Body"]
    return payload


def maybe_harvest_export_job(
    job_result: Any,
    *,
    run_id: str | None = None,
    instrument_id: str = "",
    instrument_kind: str = "",
    sample_id: str = "",
    signal_specs: Mapping[str, CurveSignalSpec] | None = None,
) -> list[NormalizedCurve]:
    """If *job_result* looks completed, harvest its output paths.

    Safe to call from GetExportJsonJob; never raises.
    """
    try:
        body = job_result
        if hasattr(job_result, "Result"):
            body = job_result.Result
        if isinstance(body, dict) and "Payload" in body:
            body = body["Payload"]
        body = dig_body(body)
        if not isinstance(body, dict):
            return []
        status = str(
            body.get("status") or body.get("Status") or body.get("state") or ""
        ).lower()
        if status and status not in ("completed", "complete", "succeeded", "success", "done"):
            # Some responses omit status and only return outputs when done.
            if not (body.get("outputs") or body.get("Outputs")):
                return []
        paths = extract_output_paths(body)
        if not paths:
            return []
        rid = run_id or str(body.get("jobId") or body.get("JobId") or "unknown")
        return harvest_export_paths(
            paths,
            run_id=rid,
            instrument_id=instrument_id or os.environ.get("LAB_INSTRUMENT_ID", ""),
            instrument_kind=instrument_kind
            or os.environ.get("LAB_INSTRUMENT_KIND", ""),
            sample_id=sample_id,
            signal_specs=signal_specs,
        )
    except Exception:
        _log.exception("maybe_harvest_export_job failed")
        return []
