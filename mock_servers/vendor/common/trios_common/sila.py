"""Shared SiLA helpers for TRIOS feature implementations."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from typing import Any

from sila_common.codec import dumps_json, loads_mapping
from sila_common.envelope import Code, failed, rejected, running, succeeded

from .http import TriosClientError

STATUS_PROPERTY_POLL_INTERVAL_SECONDS = 0.5
WAIT_STATUS_POLL_INTERVAL_SECONDS = 0.5
RUN_POLL_INTERVAL = 2.0
RUN_START_TIMEOUT = 10.0


def map_trios(err_cls: type[Any], exc: BaseException) -> Any:
    return err_cls(str(exc))


def _trios_details(exc: TriosClientError) -> dict[str, Any]:
    return {
        "status_code": exc.status_code,
        "url": exc.url,
        "canForce": exc.can_force,
    }


def trios_error_envelope(exc: TriosClientError, *, correlation_id: str = "") -> dict[str, Any]:
    """Map a TRIOS HTTP error to Failed (5xx / transport) or Rejected (4xx)."""
    builder = rejected if exc.status_code is not None and 400 <= exc.status_code < 500 else failed
    return builder(
        code=Code.trios_http(exc.status_code),
        message=exc.message,
        details=_trios_details(exc),
        correlation_id=correlation_id,
    )


def envelope_call(
    client_fn: Callable[..., Any],
    *,
    payload: Any = ...,
    correlation_id: str = "",
    client_error_cls: type[BaseException] = TriosClientError,
    wrap_payload: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Run a REST call and wrap the result as a SiLA Envelope structure.

    *wrap_payload* is applied to successful REST results before encoding
    (e.g. ``RestBodyResult(Body=...)``). Errors are unchanged.
    """
    wrap = wrap_payload or (lambda value: value)
    try:
        result = client_fn() if payload is ... else client_fn(payload)
        return succeeded(payload=wrap(result), correlation_id=correlation_id)
    except client_error_cls as exc:
        if isinstance(exc, TriosClientError):
            return trios_error_envelope(exc, correlation_id=correlation_id)
        return failed(code=Code.trios_http(None), message=str(exc), correlation_id=correlation_id)
    except (json.JSONDecodeError, ValueError) as exc:
        return rejected(
            code=Code.REQUEST_INVALID_PAYLOAD,
            message=str(exc),
            correlation_id=correlation_id,
        )


def json_call(
    client_fn: Callable[..., Any],
    response_cls: type,
    err_cls: type,
    *,
    payload_json: str | None = None,
    client_error_cls: type[BaseException] = TriosClientError,
) -> Any:
    """Run a REST call and wrap the result as ``response_cls(ResultJson=...)``."""
    try:
        if payload_json is not None:
            result = client_fn(loads_mapping(payload_json))
        else:
            result = client_fn()
        return response_cls(ResultJson=dumps_json(result))
    except client_error_cls as e:
        raise map_trios(err_cls, e) from e
    except (json.JSONDecodeError, ValueError) as e:
        raise map_trios(err_cls, client_error_cls(str(e))) from e


def start_status_property_poller(
    fetch_json: Callable[[], str],
    queue: Queue[Any],
    err_cls: type[Any],
    *,
    client_error_cls: type[BaseException] = TriosClientError,
    thread_name: str = "trios-status-poller",
    interval_s: float = STATUS_PROPERTY_POLL_INTERVAL_SECONDS,
) -> None:
    """Daemon thread that polls REST and pushes JSON (or errors) into *queue*."""

    def _run() -> None:
        while True:
            try:
                queue.put(fetch_json())
            except client_error_cls as exc:
                queue.put(err_cls(str(exc)))
                return
            except Exception as exc:  # noqa: BLE001 — surface unexpected poller errors
                queue.put(err_cls(str(exc)))
                return
            time.sleep(interval_s)

    threading.Thread(target=_run, daemon=True, name=thread_name).start()


def queue_run_is_active(queue_status: Any) -> bool:
    """Return True when GET /queue/status shows data.isRunning."""
    if not isinstance(queue_status, dict):
        return False
    data = queue_status.get("data", queue_status)
    if not isinstance(data, dict):
        return False
    return bool(data.get("isRunning", False))


def queue_run_is_done(queue_status: Any) -> bool:
    """Return True when GET /queue/status shows the queue run has finished."""
    return not queue_run_is_active(queue_status)


def run_wait_with_status_stream(
    wait_call: Callable[[], Any],
    get_instrument_status: Callable[[], Any],
    instance: Any,
    intermediate_cls: type,
    *,
    client_error_cls: type[BaseException] = TriosClientError,
    err_cls: type[Any],
    poll_interval_s: float = WAIT_STATUS_POLL_INTERVAL_SECONDS,
) -> str:
    """Run a blocking wait REST call on a worker while streaming GET /status."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future: Future[Any] = pool.submit(wait_call)
        while not future.done():
            try:
                status = get_instrument_status()
                instance.send_intermediate_response(
                    intermediate_cls(StatusJson=dumps_json(status))
                )
            except client_error_cls:
                # Transient status poll failures should not abort the wait.
                pass
            if future.done():
                break
            time.sleep(poll_interval_s)
        try:
            result = future.result()
        except client_error_cls as e:
            raise map_trios(err_cls, e) from e
    return dumps_json(result)


def poll_experiment_until_done(
    get_queue_status: Callable[[], Any],
    instance: Any,
    intermediate_cls: type,
    *,
    poll_interval_s: float = RUN_POLL_INTERVAL,
    start_timeout_s: float = RUN_START_TIMEOUT,
) -> None:
    """Two-phase poll of GET /queue/status until the experiment finishes.

    Phase 1 – wait up to *start_timeout_s* for data.isRunning to become true.
    Phase 2 – once started, keep streaming queue status until isRunning is false.
    """
    start_deadline = time.monotonic() + start_timeout_s
    while True:
        time.sleep(poll_interval_s)
        status = get_queue_status()
        instance.send_intermediate_response(intermediate_cls(StatusJson=dumps_json(status)))
        if queue_run_is_active(status):
            break
        if time.monotonic() >= start_deadline:
            # Instant experiment (started and finished between polls) or start never began.
            return

    while True:
        time.sleep(poll_interval_s)
        status = get_queue_status()
        instance.send_intermediate_response(intermediate_cls(StatusJson=dumps_json(status)))
        if queue_run_is_done(status):
            break


def run_wait_with_envelope_stream(
    wait_call: Callable[[], Any],
    get_instrument_status: Callable[[], Any],
    instance: Any,
    intermediate_cls: type,
    *,
    client_error_cls: type[BaseException] = TriosClientError,
    correlation_id: str = "",
    poll_interval_s: float = WAIT_STATUS_POLL_INTERVAL_SECONDS,
    wrap_payload: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Run a blocking wait REST call while streaming ``running`` envelopes."""
    wrap = wrap_payload or (lambda value: value)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future: Future[Any] = pool.submit(wait_call)
        while not future.done():
            try:
                status = get_instrument_status()
                instance.send_intermediate_response(
                    intermediate_cls(
                        Update=running(payload=wrap(status), correlation_id=correlation_id)
                    )
                )
            except client_error_cls:
                pass
            if future.done():
                break
            time.sleep(poll_interval_s)
        try:
            result = future.result()
        except client_error_cls as exc:
            if isinstance(exc, TriosClientError):
                return trios_error_envelope(exc, correlation_id=correlation_id)
            return failed(code=Code.trios_http(None), message=str(exc), correlation_id=correlation_id)
    return succeeded(payload=wrap(result), correlation_id=correlation_id)


def poll_experiment_with_envelope(
    get_queue_status: Callable[[], Any],
    instance: Any,
    intermediate_cls: type,
    *,
    poll_interval_s: float = RUN_POLL_INTERVAL,
    start_timeout_s: float = RUN_START_TIMEOUT,
    correlation_id: str = "",
    client_error_cls: type[BaseException] = TriosClientError,
    wrap_payload: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Two-phase poll of GET /queue/status, streaming ``running`` envelopes."""
    wrap = wrap_payload or (lambda value: value)
    start_deadline = time.monotonic() + start_timeout_s
    status: Any = None
    try:
        while True:
            time.sleep(poll_interval_s)
            status = get_queue_status()
            instance.send_intermediate_response(
                intermediate_cls(Update=running(payload=wrap(status), correlation_id=correlation_id))
            )
            if queue_run_is_active(status):
                break
            if time.monotonic() >= start_deadline:
                return succeeded(payload=wrap(status), correlation_id=correlation_id)

        while True:
            time.sleep(poll_interval_s)
            status = get_queue_status()
            instance.send_intermediate_response(
                intermediate_cls(Update=running(payload=wrap(status), correlation_id=correlation_id))
            )
            if queue_run_is_done(status):
                break
    except client_error_cls as exc:
        if isinstance(exc, TriosClientError):
            return trios_error_envelope(exc, correlation_id=correlation_id)
        return failed(code=Code.trios_http(None), message=str(exc), correlation_id=correlation_id)
    return succeeded(payload=wrap(status), correlation_id=correlation_id)


AUTOSAMPLER_POLL_INTERVAL = 0.5
AUTOSAMPLER_START_TIMEOUT = 5.0


def autosampler_is_moving(status: Any) -> bool:
    """Return True when GET /autosampler/status shows ``data.isMoving``."""
    if not isinstance(status, dict):
        return False
    data = status.get("data", status)
    if not isinstance(data, dict):
        return False
    return bool(data.get("isMoving", False))


def poll_autosampler_with_envelope(
    get_autosampler_status: Callable[[], Any],
    instance: Any,
    intermediate_cls: type,
    *,
    poll_interval_s: float = AUTOSAMPLER_POLL_INTERVAL,
    start_timeout_s: float = AUTOSAMPLER_START_TIMEOUT,
    correlation_id: str = "",
    client_error_cls: type[BaseException] = TriosClientError,
    wrap_payload: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Two-phase poll of GET /autosampler/status; Result Payload is terminal status.

    Phase 1 waits for ``isMoving`` true. If motion is never observed within
    *start_timeout_s*, returns ``Failed`` / ``trios.autosampler.motion_not_started``.
    Phase 2 streams until ``isMoving`` is false and returns that terminal snapshot.
    """
    wrap = wrap_payload or (lambda value: value)
    start_deadline = time.monotonic() + start_timeout_s
    status: Any = None
    try:
        while True:
            time.sleep(poll_interval_s)
            status = get_autosampler_status()
            instance.send_intermediate_response(
                intermediate_cls(Update=running(payload=wrap(status), correlation_id=correlation_id))
            )
            if autosampler_is_moving(status):
                break
            if time.monotonic() >= start_deadline:
                return failed(
                    code=Code.TRIOS_AUTOSAMPLER_MOTION_NOT_STARTED,
                    message="Autosampler motion was not observed within 5 seconds of the accepted REST request.",
                    payload=wrap(status),
                    correlation_id=correlation_id,
                )

        while True:
            time.sleep(poll_interval_s)
            status = get_autosampler_status()
            instance.send_intermediate_response(
                intermediate_cls(Update=running(payload=wrap(status), correlation_id=correlation_id))
            )
            if not autosampler_is_moving(status):
                break
    except client_error_cls as exc:
        if isinstance(exc, TriosClientError):
            return trios_error_envelope(exc, correlation_id=correlation_id)
        return failed(code=Code.trios_http(None), message=str(exc), correlation_id=correlation_id)
    return succeeded(payload=wrap(status), correlation_id=correlation_id)
