"""Persist a completed OPUS measurement.

Peak picking stays in front of this module. ``persist`` writes the measurement
record in order: processed JSON, peak rows, the NAS copy, then Loki events.
A configured sink error stops the rest, appends the error to
``raw_response``, and sets ``success`` false. Unset sinks are skipped.
"""

from __future__ import annotations

import logging
from typing import Any

from ftir_automation.nas import maybe_upload_measurement
from ftir_automation.peaks import processed_json_path, store_peaks, write_processed_json
from ftir_automation.telemetry import (
    emit_measure_result,
    emit_processed_spectrum,
    emit_spectrum_blocks,
)

_log = logging.getLogger(__name__)


def persist(
    result: Any,
    picked: Any | None,
    *,
    run_id: str,
    sample_id: str = "",
    operator: str = "",
    project: str = "",
    output_dir: str = "",
) -> Any:
    """Write the measurement record. Returns *result*, possibly with success cleared."""
    steps = (
        (
            "processed JSON",
            lambda: _write_processed(result, picked, output_dir=output_dir),
        ),
        (
            "peak SQL",
            lambda: _store_peaks(
                picked,
                sample_id=sample_id,
                run_id=run_id,
                operator=operator,
                project=project,
            ),
        ),
        ("NAS", lambda: maybe_upload_measurement(result)),
        (
            "Loki",
            lambda: _emit_loki(
                result,
                picked,
                run_id=run_id,
                sample_id=sample_id,
                operator=operator,
                project=project,
            ),
        ),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:
            return _fail(result, name, exc)
    return result


def _write_processed(result: Any, picked: Any | None, *, output_dir: str) -> None:
    if picked is None:
        return
    path = processed_json_path(
        file_path=str(getattr(result, "file_path", "") or ""),
        output_dir=output_dir,
    )
    if not path:
        return
    write_processed_json(path, picked)
    _log.info("Wrote processed spectrum: %s", path)


def _store_peaks(
    picked: Any | None,
    *,
    sample_id: str,
    run_id: str,
    operator: str,
    project: str,
) -> None:
    if picked is None:
        return
    n = store_peaks(
        picked,
        sample_id=sample_id,
        run_id=run_id,
        operator=operator,
        project=project,
    )
    if n:
        _log.info(
            "Stored %d peaks in polymer_dev for run_id=%s sample_id=%s",
            n,
            run_id,
            sample_id,
        )


def _emit_loki(
    result: Any,
    picked: Any | None,
    *,
    run_id: str,
    sample_id: str,
    operator: str,
    project: str,
) -> None:
    emit_spectrum_blocks(
        getattr(result, "spectra", None) or [],
        run_id=run_id,
        sample_id=sample_id,
    )
    if picked is not None:
        emit_processed_spectrum(picked, run_id=run_id, sample_id=sample_id)
    emit_measure_result(
        run_id=run_id,
        sample_id=sample_id,
        operator=operator,
        project=project,
        file_path=str(getattr(result, "file_path", "") or ""),
        asm_path=str(getattr(result, "asm_path", "") or ""),
        spectra=getattr(result, "spectra", None) or [],
        raw_response=getattr(result, "raw_response", None) or [],
        parameters=getattr(result, "parameters", None) or {},
    )


def _fail(result: Any, sink: str, exc: BaseException) -> Any:
    _log.exception("measurement record failed at %s", sink)
    message = f"{sink}: {exc}"
    raw = list(getattr(result, "raw_response", None) or [])
    raw.append(message)
    result.raw_response = raw
    result.success = False
    return result
