"""
SiLA feature implementations bridging GpcAutomation 1:1 (FeatureVersion 2.0).

All UIA operations are serialized through a single-threaded executor because
pywinauto's UIA backend talks to COM objects that should not be touched from
arbitrary threads in the SiLA gRPC threadpool.

The state machine (`gpc.sila.state_machine.GpcStateMachine`) is shared across
all features. Invalid-state and automation failures return Envelope
Rejected / Failed; only ``InternalError`` is raised when an envelope cannot
be built.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Callable, Mapping, TypeVar

from sila2.server import MetadataDict, SilaServer
from sila_common.envelope import Code, failed, parse_request, rejected, succeeded

from gpc_automation import GpcAutomation

from .state_machine import GpcState, GpcStateMachine, InvalidStateTransition

from .gpc_sila.instrumentlifecycle.instrumentlifecycle_base import (
    InstrumentLifecycleBase,
)
from .gpc_sila.instrumentlifecycle.instrumentlifecycle_types import (
    BringToFront_Responses,
    Connect_Responses,
    Disconnect_Responses,
    EnterMaintenance_Responses,
    ExitMaintenance_Responses,
    Pause_Responses,
    Resume_Responses,
    Stop_Responses,
)
from .gpc_sila.instrumentstatus.instrumentstatus_base import InstrumentStatusBase
from .gpc_sila.instrumentstatus.instrumentstatus_types import (
    GetAnalysisStatus_Responses,
    GetColor_Responses,
    GetCurrentSequenceStep_Responses,
    GetInstrumentSnapshot_Responses,
    GetSequenceSummary_Responses,
    GetStartButtonState_Responses,
    GetSystemInfo_Responses,
    GetSystemStatus_Responses,
    GetText_Responses,
    InvalidateCache_Responses,
)

from .gpc_sila.sequencecontrol.sequencecontrol_base import SequenceControlBase
from .gpc_sila.sequencecontrol.sequencecontrol_types import ClickStart_Responses

from .gpc_sila.uiinput.uiinput_base import UiInputBase
from .gpc_sila.uiinput.uiinput_types import (
    ClickAt_Responses,
    ClickControlByLocator_Responses,
    ClickMenu_Responses,
    FindControl_Responses,
    Hotkey_Responses,
    ListChildren_Responses,
    PrintControlTree_Responses,
    TypeIntoControlByLocator_Responses,
    TypeText_Responses,
    WaitForWindow_Responses,
    WaitUntilGone_Responses,
)

from .gpc_sila.utility.utility_base import UtilityBase
from .gpc_sila.utility.utility_types import Screenshot_Responses

log = logging.getLogger(__name__)

T = TypeVar("T")

CONNECTED_STATES = (
    GpcState.CONNECT,
    GpcState.RUN,
    GpcState.PAUSE,
    GpcState.MAINTENANCE,
)


# ---------------------------------------------------------------------------
# Shared executor + helpers
# ---------------------------------------------------------------------------


class GpcExecutor:
    """
    Single-thread executor that owns the `GpcAutomation` instance.

    All UIA calls are routed through this executor so that pywinauto / COM
    objects are only touched from one thread, regardless of how many SiLA
    gRPC worker threads are dispatching commands concurrently.
    """

    def __init__(self, gpc: GpcAutomation) -> None:
        self.gpc = gpc
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpc-uia")

    def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self._pool.submit(fn, *args, **kwargs).result()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


def _state_msg(exc: InvalidStateTransition) -> str:
    return str(exc)


def _payload_dict(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise ValueError("Request.Payload must be a mapping or Void.")


def _payload_get(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Request.Payload must include a non-empty string {key}.")
    return value


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _as_bool(value: Any, default: bool = False) -> bool:
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


def _rejected_state(exc: InvalidStateTransition, *, correlation_id: str) -> dict[str, Any]:
    return rejected(
        code=Code.GPC_STATE_INVALID,
        message=_state_msg(exc),
        correlation_id=correlation_id,
    )


def _rejected_payload(message: str, *, correlation_id: str) -> dict[str, Any]:
    return rejected(
        code=Code.REQUEST_INVALID_PAYLOAD,
        message=message,
        correlation_id=correlation_id,
    )


def _failed_automation(exc: BaseException, *, correlation_id: str) -> dict[str, Any]:
    return failed(
        code=Code.GPC_AUTOMATION,
        message=str(exc),
        correlation_id=correlation_id,
    )


def _parse_locator(locator: Any) -> dict[str, Any]:
    if locator is None:
        return {}
    if isinstance(locator, str):
        # Accept legacy JSON-string form during migration.
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


def _parse_string_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        from .codec import loads_list

        value = loads_list(value)
    if not isinstance(value, list):
        raise ValueError(f"Payload.{field} must be a list of strings.")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"Payload.{field} must be a list of strings.")
    return list(value)


# ---------------------------------------------------------------------------
# InstrumentLifecycle
# ---------------------------------------------------------------------------


class InstrumentLifecycleImpl(InstrumentLifecycleBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm
        self._fsm._on_change = self._publish_state  # type: ignore[attr-defined]
        self.update_CurrentState(self._fsm.state.value)

    def _publish_state(self, state: GpcState) -> None:
        self.update_CurrentState(state.value)

    def Connect(self, Request: Any, *, metadata: MetadataDict) -> Connect_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.DISCONNECTED)
        except InvalidStateTransition as e:
            return Connect_Responses(Result=_rejected_state(e, correlation_id=req.correlation_id))
        try:
            self._exec.run(self._exec.gpc.connect)
        except Exception as e:
            log.exception("Connect failed")
            return Connect_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.CONNECT)
        return Connect_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Disconnect(self, Request: Any, *, metadata: MetadataDict) -> Disconnect_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.CONNECT)
        except InvalidStateTransition as e:
            return Disconnect_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.invalidate_cache)
            self._exec.gpc.app = None
            self._exec.gpc.main_window = None
        except Exception as e:
            log.exception("Disconnect failed")
            return Disconnect_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.DISCONNECTED)
        return Disconnect_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def BringToFront(self, Request: Any, *, metadata: MetadataDict) -> BringToFront_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(*CONNECTED_STATES)
        except InvalidStateTransition as e:
            return BringToFront_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.bring_to_front)
        except Exception as e:
            return BringToFront_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return BringToFront_Responses(
            Result=succeeded(
                payload={"status": "ok"},
                correlation_id=req.correlation_id,
            )
        )

    def EnterMaintenance(
        self, Request: Any, *, metadata: MetadataDict
    ) -> EnterMaintenance_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.MAINTENANCE)
        except InvalidStateTransition as e:
            return EnterMaintenance_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        return EnterMaintenance_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def ExitMaintenance(
        self, Request: Any, *, metadata: MetadataDict
    ) -> ExitMaintenance_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.CONNECT)
        except InvalidStateTransition as e:
            return ExitMaintenance_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        return ExitMaintenance_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Pause(self, Request: Any, *, metadata: MetadataDict) -> Pause_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.PAUSE)
        except InvalidStateTransition as e:
            return Pause_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        return Pause_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Resume(self, Request: Any, *, metadata: MetadataDict) -> Resume_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.RUN)
        except InvalidStateTransition as e:
            return Resume_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        return Resume_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Stop(self, Request: Any, *, metadata: MetadataDict) -> Stop_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.RUN, GpcState.PAUSE)
        except InvalidStateTransition as e:
            return Stop_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        clicked = False
        try:
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
            if is_on:
                self._exec.run(self._exec.gpc.click_start)
                clicked = True
        except Exception as e:
            log.exception("Stop failed while toggling Start button")
            return Stop_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.CONNECT)
        return Stop_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value, "clicked_start": clicked},
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# InstrumentStatus
# ---------------------------------------------------------------------------


class InstrumentStatusImpl(InstrumentStatusBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm
        self.update_CurrentSystemStatus("unknown")
        self.update_CurrentAnalysisStatus("unknown")

    def _require_connected_env(self, correlation_id: str) -> dict[str, Any] | None:
        try:
            self._fsm.require(*CONNECTED_STATES)
        except InvalidStateTransition as e:
            return _rejected_state(e, correlation_id=correlation_id)
        return None

    def GetColor(self, Request: Any, *, metadata: MetadataDict) -> GetColor_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetColor_Responses(Result=blocked)
        try:
            auto_id = _require_str(_payload_dict(req.payload), "AutoId")
        except ValueError as e:
            return GetColor_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            color = self._exec.run(self._exec.gpc.get_color, auto_id)
        except Exception as e:
            return GetColor_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetColor_Responses(
            Result=succeeded(
                payload={"color": color},
                correlation_id=req.correlation_id,
            )
        )

    def GetText(self, Request: Any, *, metadata: MetadataDict) -> GetText_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetText_Responses(Result=blocked)
        try:
            auto_id = _require_str(_payload_dict(req.payload), "AutoId")
        except ValueError as e:
            return GetText_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            text = self._exec.run(self._exec.gpc.get_text_in_settings_window, auto_id)
        except Exception as e:
            return GetText_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetText_Responses(
            Result=succeeded(
                payload={"text": text},
                correlation_id=req.correlation_id,
            )
        )

    def GetSystemStatus(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSystemStatus_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetSystemStatus_Responses(Result=blocked)
        try:
            color = self._exec.run(self._exec.gpc.get_system_status)
        except Exception as e:
            return GetSystemStatus_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self.update_CurrentSystemStatus(color)
        return GetSystemStatus_Responses(
            Result=succeeded(
                payload={"color": color},
                correlation_id=req.correlation_id,
            )
        )

    def GetAnalysisStatus(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetAnalysisStatus_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetAnalysisStatus_Responses(Result=blocked)
        try:
            color = self._exec.run(self._exec.gpc.get_analysis_status)
        except Exception as e:
            return GetAnalysisStatus_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self.update_CurrentAnalysisStatus(color)
        return GetAnalysisStatus_Responses(
            Result=succeeded(
                payload={"color": color},
                correlation_id=req.correlation_id,
            )
        )

    def GetStartButtonState(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetStartButtonState_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetStartButtonState_Responses(Result=blocked)
        try:
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
        except Exception as e:
            return GetStartButtonState_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetStartButtonState_Responses(
            Result=succeeded(
                payload={"IsOn": bool(is_on)},
                correlation_id=req.correlation_id,
            )
        )

    def GetSequenceSummary(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSequenceSummary_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetSequenceSummary_Responses(Result=blocked)
        try:
            summary = self._exec.run(self._exec.gpc.get_sequence_summary)
        except Exception as e:
            return GetSequenceSummary_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetSequenceSummary_Responses(
            Result=succeeded(
                payload=asdict(summary),
                correlation_id=req.correlation_id,
            )
        )

    def GetCurrentSequenceStep(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetCurrentSequenceStep_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetCurrentSequenceStep_Responses(Result=blocked)
        try:
            step = self._exec.run(self._exec.gpc.get_current_sequence_step)
        except Exception as e:
            return GetCurrentSequenceStep_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetCurrentSequenceStep_Responses(
            Result=succeeded(
                payload=asdict(step),
                correlation_id=req.correlation_id,
            )
        )

    def GetSystemInfo(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSystemInfo_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetSystemInfo_Responses(Result=blocked)
        try:
            modules = self._exec.run(self._exec.gpc.get_system_info)
        except Exception as e:
            return GetSystemInfo_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return GetSystemInfo_Responses(
            Result=succeeded(
                payload=[asdict(m) for m in modules],
                correlation_id=req.correlation_id,
            )
        )

    def GetInstrumentSnapshot(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetInstrumentSnapshot_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return GetInstrumentSnapshot_Responses(Result=blocked)
        try:
            snapshot = self._exec.run(self._exec.gpc.get_instrument_snapshot)
        except Exception as e:
            return GetInstrumentSnapshot_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        self.update_CurrentSystemStatus(snapshot.system_status)
        self.update_CurrentAnalysisStatus(snapshot.analysis_status)
        payload = {
            "system_status": snapshot.system_status,
            "analysis_status": snapshot.analysis_status,
            "sequence_summary": asdict(snapshot.sequence_summary),
            "current_step": asdict(snapshot.current_step),
            "system_info": [asdict(m) for m in snapshot.system_info],
        }
        return GetInstrumentSnapshot_Responses(
            Result=succeeded(payload=payload, correlation_id=req.correlation_id)
        )

    def InvalidateCache(
        self, Request: Any, *, metadata: MetadataDict
    ) -> InvalidateCache_Responses:
        req = parse_request(Request)
        blocked = self._require_connected_env(req.correlation_id)
        if blocked is not None:
            return InvalidateCache_Responses(Result=blocked)
        try:
            self._exec.run(self._exec.gpc.invalidate_cache)
        except Exception as e:
            return InvalidateCache_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return InvalidateCache_Responses(
            Result=succeeded(
                payload={"status": "ok"},
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# SequenceControl
# ---------------------------------------------------------------------------


class SequenceControlImpl(SequenceControlBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm

    def ClickStart(self, Request: Any, *, metadata: MetadataDict) -> ClickStart_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.CONNECT, GpcState.RUN)
        except InvalidStateTransition as e:
            return ClickStart_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_start)
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
        except Exception as e:
            log.exception("ClickStart failed")
            return ClickStart_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        target = GpcState.RUN if is_on else GpcState.CONNECT
        try:
            self._fsm.transition(target)
        except InvalidStateTransition as e:
            return ClickStart_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        return ClickStart_Responses(
            Result=succeeded(
                payload={"is_on": bool(is_on), "state": target.value},
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# UiInput  (Maintenance only)
# ---------------------------------------------------------------------------


class UiInputImpl(UiInputBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm

    def _require_maintenance_env(self, correlation_id: str) -> dict[str, Any] | None:
        try:
            self._fsm.require(GpcState.MAINTENANCE)
        except InvalidStateTransition as e:
            return _rejected_state(e, correlation_id=correlation_id)
        return None

    def ClickMenu(self, Request: Any, *, metadata: MetadataDict) -> ClickMenu_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return ClickMenu_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            path_items = _parse_string_list(
                _payload_get(payload, "MenuPath"), field="MenuPath"
            )
            if not path_items:
                raise ValueError("Payload.MenuPath must be a non-empty list of strings.")
        except ValueError as e:
            return ClickMenu_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_menu, *path_items)
        except Exception as e:
            return ClickMenu_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return ClickMenu_Responses(
            Result=succeeded(
                payload={"status": "ok", "menu_path": path_items},
                correlation_id=req.correlation_id,
            )
        )

    def ClickAt(self, Request: Any, *, metadata: MetadataDict) -> ClickAt_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return ClickAt_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            if "X" not in payload or "Y" not in payload:
                raise ValueError("Payload must include X and Y.")
            x = _as_int(payload["X"])
            y = _as_int(payload["Y"])
            clicks = max(1, _as_int(_payload_get(payload, "Clicks", default=1), 1))
        except (TypeError, ValueError) as e:
            return ClickAt_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_at, x, y, clicks)
        except Exception as e:
            return ClickAt_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return ClickAt_Responses(
            Result=succeeded(
                payload={"status": "ok", "x": x, "y": y, "clicks": clicks},
                correlation_id=req.correlation_id,
            )
        )

    def TypeText(self, Request: Any, *, metadata: MetadataDict) -> TypeText_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return TypeText_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            text = payload.get("Text")
            if not isinstance(text, str):
                raise ValueError("Payload.Text must be a string.")
            interval_ms = _as_int(_payload_get(payload, "IntervalMs", default=0))
        except (TypeError, ValueError) as e:
            return TypeText_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        interval = (interval_ms / 1000.0) if interval_ms > 0 else 0.02
        try:
            self._exec.run(self._exec.gpc.type_text, text, interval)
        except Exception as e:
            return TypeText_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return TypeText_Responses(
            Result=succeeded(
                payload={"status": "ok", "chars": len(text)},
                correlation_id=req.correlation_id,
            )
        )

    def Hotkey(self, Request: Any, *, metadata: MetadataDict) -> Hotkey_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return Hotkey_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            keys = _parse_string_list(_payload_get(payload, "Keys"), field="Keys")
            if not keys:
                raise ValueError("Payload.Keys must be a non-empty list of strings.")
        except ValueError as e:
            return Hotkey_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.hotkey, *keys)
        except Exception as e:
            return Hotkey_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return Hotkey_Responses(
            Result=succeeded(
                payload={"status": "ok", "keys": keys},
                correlation_id=req.correlation_id,
            )
        )

    def FindControl(
        self, Request: Any, *, metadata: MetadataDict
    ) -> FindControl_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return FindControl_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            kwargs = _parse_locator(_payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
        except ValueError as e:
            return FindControl_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def describe() -> dict[str, Any]:
            ctrl = self._exec.gpc.find_control(**kwargs)
            wrapper = ctrl.wrapper_object()
            rect = wrapper.rectangle()
            info = wrapper.element_info
            return {
                "exists": True,
                "name": wrapper.window_text() or "",
                "auto_id": getattr(info, "automation_id", "") or "",
                "control_type": getattr(info, "control_type", "") or "",
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
            }

        try:
            result_payload = self._exec.run(describe)
        except Exception as e:
            log.debug("FindControl: %s -> not found (%s)", kwargs, e)
            result_payload = {
                "exists": False,
                "name": "",
                "auto_id": kwargs.get("auto_id", ""),
                "control_type": kwargs.get("control_type", ""),
                "rect": [0, 0, 0, 0],
            }
        return FindControl_Responses(
            Result=succeeded(
                payload=result_payload,
                correlation_id=req.correlation_id,
            )
        )

    def ClickControlByLocator(
        self, Request: Any, *, metadata: MetadataDict
    ) -> ClickControlByLocator_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return ClickControlByLocator_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            kwargs = _parse_locator(_payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
        except ValueError as e:
            return ClickControlByLocator_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def find_and_click() -> None:
            ctrl = self._exec.gpc.find_control(**kwargs).wrapper_object()
            self._exec.gpc.click_control(ctrl)

        try:
            self._exec.run(find_and_click)
        except Exception as e:
            return ClickControlByLocator_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return ClickControlByLocator_Responses(
            Result=succeeded(
                payload={"status": "ok", "locator": kwargs},
                correlation_id=req.correlation_id,
            )
        )

    def TypeIntoControlByLocator(
        self, Request: Any, *, metadata: MetadataDict
    ) -> TypeIntoControlByLocator_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return TypeIntoControlByLocator_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            kwargs = _parse_locator(_payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
            text = payload.get("Text")
            if not isinstance(text, str):
                raise ValueError("Payload.Text must be a string.")
            clear_first = _as_bool(_payload_get(payload, "ClearFirst", default=False))
        except ValueError as e:
            return TypeIntoControlByLocator_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def find_and_type() -> None:
            ctrl = self._exec.gpc.find_control(**kwargs).wrapper_object()
            self._exec.gpc.type_into_control(ctrl, text, clear_first=clear_first)

        try:
            self._exec.run(find_and_type)
        except Exception as e:
            return TypeIntoControlByLocator_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return TypeIntoControlByLocator_Responses(
            Result=succeeded(
                payload={"status": "ok", "locator": kwargs, "chars": len(text)},
                correlation_id=req.correlation_id,
            )
        )

    def WaitForWindow(
        self, Request: Any, *, metadata: MetadataDict
    ) -> WaitForWindow_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return WaitForWindow_Responses(Result=blocked)
        try:
            title = _require_str(_payload_dict(req.payload), "Title")
        except ValueError as e:
            return WaitForWindow_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(
                self._exec.gpc.wait_for_window, title, req.timeout_seconds
            )
        except Exception as e:
            return WaitForWindow_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return WaitForWindow_Responses(
            Result=succeeded(
                payload={"appeared": True, "title": title},
                correlation_id=req.correlation_id,
            )
        )

    def WaitUntilGone(
        self, Request: Any, *, metadata: MetadataDict
    ) -> WaitUntilGone_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return WaitUntilGone_Responses(Result=blocked)
        try:
            title = _require_str(_payload_dict(req.payload), "Title")
        except ValueError as e:
            return WaitUntilGone_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        timeout = req.timeout_seconds if req.timeout_seconds is not None else 10
        try:
            self._exec.run(self._exec.gpc.wait_until_gone, title, timeout)
        except Exception as e:
            return WaitUntilGone_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return WaitUntilGone_Responses(
            Result=succeeded(
                payload={"gone": True, "title": title},
                correlation_id=req.correlation_id,
            )
        )

    def PrintControlTree(
        self, Request: Any, *, metadata: MetadataDict
    ) -> PrintControlTree_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return PrintControlTree_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            depth_raw = _as_int(_payload_get(payload, "Depth", default=0))
        except (TypeError, ValueError) as e:
            return PrintControlTree_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        depth = depth_raw if depth_raw > 0 else 3
        try:
            self._exec.run(self._exec.gpc.print_control_tree, None, depth)
        except Exception as e:
            return PrintControlTree_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return PrintControlTree_Responses(
            Result=succeeded(
                payload={"status": "ok", "depth": depth},
                correlation_id=req.correlation_id,
            )
        )

    def ListChildren(
        self, Request: Any, *, metadata: MetadataDict
    ) -> ListChildren_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return ListChildren_Responses(Result=blocked)
        try:
            payload = _payload_dict(req.payload)
            kwargs = _parse_locator(_payload_get(payload, "Locator", "LocatorJson"))
        except ValueError as e:
            return ListChildren_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def collect() -> list[dict[str, Any]]:
            if kwargs:
                parent = self._exec.gpc.find_control(**kwargs).wrapper_object()
            else:
                parent = self._exec.gpc.main_window
            results: list[dict[str, Any]] = []
            for child in parent.children():
                info = child.element_info
                results.append(
                    {
                        "name": child.window_text() or "",
                        "auto_id": getattr(info, "automation_id", "") or "",
                        "control_type": getattr(info, "control_type", "") or "",
                    }
                )
            return results

        try:
            result_payload = self._exec.run(collect)
        except Exception as e:
            return ListChildren_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return ListChildren_Responses(
            Result=succeeded(
                payload=result_payload,
                correlation_id=req.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


class UtilityImpl(UtilityBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm

    def Screenshot(self, Request: Any, *, metadata: MetadataDict) -> Screenshot_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(*CONNECTED_STATES)
        except InvalidStateTransition as e:
            return Screenshot_Responses(
                Result=_rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            payload = _payload_dict(req.payload)
            filename = payload.get("Filename")
            if filename is None:
                filename = ""
            if not isinstance(filename, str):
                raise ValueError("Payload.Filename must be a string.")
        except ValueError as e:
            return Screenshot_Responses(
                Result=_rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        target = filename or "gpc_screenshot.png"
        try:
            self._exec.run(self._exec.gpc.screenshot, target)
        except Exception as e:
            return Screenshot_Responses(
                Result=_failed_automation(e, correlation_id=req.correlation_id)
            )
        return Screenshot_Responses(
            Result=succeeded(
                payload={"filename": target},
                correlation_id=req.correlation_id,
            )
        )
