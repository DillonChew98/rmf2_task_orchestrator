"""Persist a completed X3 DSC export.

``record_export_response`` writes, in order: curve JSON beside each export,
a ``polymer_dev.runs`` row, the NAS copy, then Loki ``x3dsc.curve`` lines.
A configured sink error stops the rest and raises :class:`RecordError`.
Unset sinks are skipped. Files are not uploaded to MinIO.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from trios_common.harvest import (
    curve_to_json_dict,
    dig_body,
    extract_output_paths,
    load_export_json,
    normalize_export,
)

from x3dsc_automation.nas import maybe_upload_paths
from x3dsc_automation.runs_store import store_run
from x3dsc_automation.telemetry import emit_curves

_log = logging.getLogger(__name__)

_ID_KEYS = ("experimentId", "ExperimentId", "currentExperimentId")
_DONE = frozenset({"completed", "complete", "succeeded", "success", "done"})


class RecordError(Exception):
    """A configured measurement-record sink failed."""

    def __init__(self, sink: str, cause: BaseException) -> None:
        self.sink = sink
        self.cause = cause
        super().__init__(f"{sink}: {cause}")


def resolve_run_id(*, job_body: Any, request_payload: Any, job_id: str) -> str:
    """Prefer the experiment id the dashboard run list already uses.

    Order: job body, then the SiLA request payload, then *job_id*. A JobId
    fallback is logged because it will not match ``currentExperimentId``.
    """
    for source in (job_body, request_payload):
        found = _id_from(source)
        if found:
            return found
    fallback = (job_id or "").strip() or "unknown"
    _log.warning(
        "export record run_id fell back to JobId %s; dashboard runs use currentExperimentId",
        fallback,
    )
    return fallback


def record_export_response(
    job_result: Any,
    *,
    request_payload: Any = None,
    job_id: str = "",
) -> str | None:
    """Persist a completed export job. Returns the run id, or None when skipped.

    Raises :class:`RecordError` when a configured sink fails.
    """
    body = _completed_body(job_result)
    if not isinstance(body, dict):
        return None
    paths = extract_output_paths(body)
    if not paths:
        return None
    run_id = resolve_run_id(
        job_body=body,
        request_payload=request_payload,
        job_id=job_id or str(body.get("jobId") or body.get("JobId") or ""),
    )
    persist_export(paths, run_id=run_id)
    return run_id


def persist_export(output_paths: list[str], *, run_id: str) -> None:
    """Write curve JSON, the runs row, the NAS copy, and Loki lines."""
    steps = (
        ("curve JSON", lambda: _write_curves(output_paths, run_id=run_id)),
        ("runs SQL", lambda state: _store(state, run_id=run_id)),
        ("NAS", lambda state: maybe_upload_paths(state["upload_paths"])),
        ("Loki", lambda state: emit_curves(state["docs"])),
    )
    state: dict[str, Any] = {"docs": [], "upload_paths": [], "sample_id": "", "operator": "", "project": ""}
    for name, step in steps:
        try:
            if name == "curve JSON":
                state = step()
            else:
                step(state)
        except RecordError:
            raise
        except Exception as exc:
            _log.exception("measurement record failed at %s", name)
            raise RecordError(name, exc) from exc


def _write_curves(output_paths: list[str], *, run_id: str) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    upload_paths: list[str] = []
    sample_id = ""
    operator = ""
    project = ""
    for path in output_paths:
        if not path or not Path(path).is_file():
            _log.warning("curve JSON skipped: missing export file %s", path)
            continue
        doc = load_export_json(path)
        curve = normalize_export(doc, path=path)
        if not sample_id:
            sample_id = curve.sample_id
        if isinstance(doc, dict):
            operator = operator or _text(doc, "operator", "Operator")
            project = project or _text(doc, "project", "Project")
            sample_id = sample_id or _text(doc, "sampleName", "SampleName", "sample_id")
        curve_doc = curve_to_json_dict(curve, run_id=run_id, instrument_kind="x3dsc")
        curve_doc["event"] = "x3dsc.curve"
        if sample_id and not curve_doc.get("sample_id"):
            curve_doc["sample_id"] = sample_id
        curve_path = str(Path(path).with_name(f"curve_{curve.channel}.json"))
        Path(curve_path).write_text(
            json.dumps(curve_doc), encoding="utf-8"
        )
        _log.info("Wrote curve JSON: %s", curve_path)
        docs.append(curve_doc)
        upload_paths.append(path)
        upload_paths.append(curve_path)
        tri = _sibling_tri(path)
        if tri:
            upload_paths.append(tri)
    return {
        "docs": docs,
        "upload_paths": upload_paths,
        "sample_id": sample_id,
        "operator": operator,
        "project": project,
    }


def _store(state: dict[str, Any], *, run_id: str) -> None:
    if not state["docs"]:
        return
    store_run(
        run_id=run_id,
        sample_id=str(state.get("sample_id") or ""),
        operator=str(state.get("operator") or ""),
        project=str(state.get("project") or ""),
    )


def _sibling_tri(path: str) -> str | None:
    tri = Path(path)
    for cand in (
        tri.with_suffix(".tri"),
        tri.with_name(tri.name.replace(".json.gz", ".tri").replace(".json", ".tri")),
    ):
        if cand.is_file():
            return str(cand)
    return None


def _completed_body(job_result: Any) -> Any:
    body = job_result
    if hasattr(job_result, "Result"):
        body = job_result.Result
    if isinstance(body, dict) and "Outcome" in body:
        if str(body.get("Outcome") or "") != "Succeeded":
            return None
        from sila_common.envelope import loads_payload

        body = loads_payload(body.get("Payload"))
    body = dig_body(body)
    if not isinstance(body, dict):
        return None
    status = str(body.get("status") or body.get("Status") or body.get("state") or "").lower()
    if status and status not in _DONE:
        if not (body.get("outputs") or body.get("Outputs")):
            return None
    return body


def _id_from(source: Any) -> str:
    if not isinstance(source, dict):
        return ""
    for key in _ID_KEYS:
        raw = source.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        if text:
            return text
    nested = source.get("Body")
    if isinstance(nested, dict):
        return _id_from(nested)
    return ""


def _text(doc: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = doc.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    info = doc.get("sampleInfo") or doc.get("SampleInfo")
    if isinstance(info, dict):
        for key in keys:
            raw = info.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return ""
