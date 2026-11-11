"""Shared SiLA command helpers: payload parsing and envelope mapping."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sila_common.envelope import Code, failed, parse_request, rejected, succeeded

from .state_machine import GpcState, GpcStateMachine, InvalidStateTransition

CONNECTED_STATES = (
    GpcState.CONNECT,
    GpcState.RUN,
    GpcState.PAUSE,
    GpcState.MAINTENANCE,
)


def payload_dict(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise ValueError("Request.Payload must be a mapping or Void.")


def payload_get(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Request.Payload must include a non-empty string {key}.")
    return value


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    return bool(value)


def rejected_state(exc: InvalidStateTransition, *, correlation_id: str) -> dict[str, Any]:
    return rejected(
        code=Code.GPC_STATE_INVALID,
        message=str(exc),
        correlation_id=correlation_id,
    )


def rejected_payload(message: str, *, correlation_id: str) -> dict[str, Any]:
    return rejected(
        code=Code.REQUEST_INVALID_PAYLOAD,
        message=message,
        correlation_id=correlation_id,
    )


def failed_automation(exc: BaseException, *, correlation_id: str) -> dict[str, Any]:
    return failed(
        code=Code.GPC_AUTOMATION,
        message=str(exc),
        correlation_id=correlation_id,
    )


def parse_locator(locator: Any) -> dict[str, Any]:
    if locator is None:
        return {}
    if isinstance(locator, str):
        from .codec import loads_mapping

        locator = loads_mapping(locator)
    if not isinstance(locator, Mapping):
        raise ValueError("Payload.Locator must be an object.")
    kwargs: dict[str, Any] = {}
    for key in ("title", "auto_id", "control_type"):
        value = locator.get(key)
        if isinstance(value, str) and value:
            kwargs[key] = value
    return kwargs


def parse_string_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        from .codec import loads_list

        value = loads_list(value)
    if not isinstance(value, list):
        raise ValueError(f"Payload.{field} must be a list of strings.")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"Payload.{field} must be a list of strings.")
    return list(value)


def handle_command(
    *,
    request: Any,
    fsm: GpcStateMachine,
    allowed: tuple[GpcState, ...],
    work: Callable[[dict[str, Any], str], Any],
) -> dict[str, Any]:
    """
    Parse request, enforce FSM guard, run *work*, map outcomes to Envelope.

    *work* receives ``(payload_dict, correlation_id)`` and returns the success
    payload body. Raise ``ValueError`` for invalid payload; other exceptions
    become ``GPC_AUTOMATION`` Failed.
    """
    req = parse_request(request)
    correlation_id = req.correlation_id
    try:
        fsm.require(*allowed)
        body = work(payload_dict(req.payload), correlation_id)
        return succeeded(payload=body, correlation_id=correlation_id)
    except InvalidStateTransition as exc:
        return rejected_state(exc, correlation_id=correlation_id)
    except ValueError as exc:
        return rejected_payload(str(exc), correlation_id=correlation_id)
    except Exception as exc:
        return failed_automation(exc, correlation_id=correlation_id)
