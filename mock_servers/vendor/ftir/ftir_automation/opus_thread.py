"""Background-thread launch + poll helper for OPUS long-running functions.

OPUS functions like ``MeasureSample`` / ``MeasureReference`` run on a
background thread inside OPUS itself; the client must poll for progress and
completion via ``command.get_thread_progress`` / ``get_thread_results``.
This module isolates that polling loop from :class:`ftir_automation.core.FtirAutomation`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from ftir_automation.telemetry import get_telemetry, sc

log = logging.getLogger(__name__)

# How often (seconds) to poll a background measurement thread for progress
_POLL_INTERVAL = 1.0
# Maximum seconds to wait for a background measurement before giving up
_MEASURE_TIMEOUT = 600


def _response_errors(lines: list) -> list[str]:
    """Return the error lines reported by OPUS in a thread result list.

    OPUS reports failures as plain text lines (e.g. ``'Error in Thread:
    error code = 9'`` or ``'Illegal Parameter Value: APF=0'``) instead of
    raising, so the response must be inspected explicitly.
    """
    errors: list[str] = []
    for line in lines or []:
        text = str(line).strip()
        if not text:
            continue
        lowered = text.lower()
        if "error" in lowered or "no measurement possible" in lowered or "illegal" in lowered:
            errors.append(text)
    return errors


def run_threaded_function(
    opus_api: Any,
    fn_name: str,
    params: dict,
    *,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> tuple[bool, list]:
    """Launch an OPUS function in a background thread and poll until done.

    Args:
        opus_api:    Connected ``opuspy_api.OpusAPI`` instance.
        fn_name:     OPUS function name (e.g. ``MeasureSample``).
        params:      OPUS parameter dict.
        on_progress: Optional callback ``(percent, status_text)`` when progress changes.

    Returns:
        ``(completed, result_lines)`` — *completed* is ``False`` on launch
        failure, polling error, or timeout.
    """
    tel = get_telemetry()
    started = time.perf_counter()
    span_cm, span = tel.start_span(
        f"opus.function {fn_name}", **{sc.ATTR_OPUS_FUNCTION: fn_name}
    )
    outcome = sc.OUTCOME_OK
    try:
        try:
            from opuspy_api.opus_commands.errors import OpusAPIError  # type: ignore[import]
        except ModuleNotFoundError:
            OpusAPIError = Exception  # type: ignore[assignment,misc]

        try:
            success, response = opus_api.command.command_line(
                opus_function_name=fn_name,
                file_id=None,
                block_name=None,
                function_parameters=params,
                background_thread=True,
            )
        except OpusAPIError as exc:
            log.error("%s command failed to launch: %s", fn_name, exc)
            outcome = sc.OUTCOME_OPUS_ERROR
            tel.set_span_error(span, str(exc), outcome=outcome)
            return False, [str(exc)]

        if not success:
            log.error("%s launch returned failure: %s", fn_name, response)
            outcome = sc.OUTCOME_OPUS_ERROR
            tel.set_span_error(span, str(response), outcome=outcome)
            return False, list(response)

        thread_id = int(response[1]) if len(response) > 1 else -1
        span.set_attribute(sc.ATTR_OPUS_THREAD_ID, thread_id)
        log.info("%s thread ID: %d — polling …", fn_name, thread_id)

        elapsed = 0.0
        last_progress = -1
        while elapsed < _MEASURE_TIMEOUT:
            time.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            try:
                progress, status_text = opus_api.command.get_thread_progress(thread_id)
                if progress != last_progress:
                    log.info("%s progress: %d%% — %s", fn_name, progress, status_text)
                    span.add_event(
                        "opus.progress",
                        {
                            sc.ATTR_OPUS_PROGRESS_PERCENT: int(progress),
                            "status": str(status_text),
                        },
                    )
                    last_progress = progress
                    if on_progress is not None:
                        try:
                            on_progress(int(progress), str(status_text))
                        except Exception:
                            log.exception("on_progress callback failed")

                done, results = opus_api.command.get_thread_results(thread_id)
                if done:
                    log.info("%s finished (%.0fs) — results: %s", fn_name, elapsed, results)
                    errors = _response_errors(results)
                    if errors:
                        outcome = sc.OUTCOME_OPUS_ERROR
                        tel.set_span_error(span, str(errors), outcome=outcome)
                    return True, list(results)
            except Exception:
                log.warning("Error polling thread %d", thread_id, exc_info=True)
                outcome = sc.OUTCOME_ERROR
                tel.set_span_error(span, "polling error", outcome=outcome)
                return False, ["polling error"]

        log.error("%s timed out after %.0f s", fn_name, _MEASURE_TIMEOUT)
        outcome = sc.OUTCOME_TIMEOUT
        tel.set_span_error(span, "timeout", outcome=outcome)
        try:
            opus_api.command.stop_task(thread_id)
        except Exception:
            pass
        return False, ["timeout"]
    finally:
        tel.record_function(fn_name, time.perf_counter() - started, outcome)
        span_cm.__exit__(None, None, None)
