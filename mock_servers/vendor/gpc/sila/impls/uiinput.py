"""UiInput SiLA feature implementation (Maintenance only)."""

from __future__ import annotations

import logging
from typing import Any

from sila2.server import MetadataDict, SilaServer
from sila_common.envelope import parse_request, succeeded

from ..command import (
    as_bool,
    as_int,
    failed_automation,
    parse_locator,
    parse_string_list,
    payload_dict,
    payload_get,
    rejected_payload,
    rejected_state,
    require_str,
)
from ..executor import GpcExecutor
from ..gpc_sila.uiinput.uiinput_base import UiInputBase
from ..gpc_sila.uiinput.uiinput_types import (
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
from ..state_machine import GpcState, GpcStateMachine, InvalidStateTransition

log = logging.getLogger(__name__)


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
            return rejected_state(e, correlation_id=correlation_id)
        return None

    def ClickMenu(self, Request: Any, *, metadata: MetadataDict) -> ClickMenu_Responses:
        req = parse_request(Request)
        blocked = self._require_maintenance_env(req.correlation_id)
        if blocked is not None:
            return ClickMenu_Responses(Result=blocked)
        try:
            payload = payload_dict(req.payload)
            path_items = parse_string_list(
                payload_get(payload, "MenuPath"), field="MenuPath"
            )
            if not path_items:
                raise ValueError("Payload.MenuPath must be a non-empty list of strings.")
        except ValueError as e:
            return ClickMenu_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_menu, *path_items)
        except Exception as e:
            return ClickMenu_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            if "X" not in payload or "Y" not in payload:
                raise ValueError("Payload must include X and Y.")
            x = as_int(payload["X"])
            y = as_int(payload["Y"])
            clicks = max(1, as_int(payload_get(payload, "Clicks", default=1), 1))
        except (TypeError, ValueError) as e:
            return ClickAt_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_at, x, y, clicks)
        except Exception as e:
            return ClickAt_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            text = payload.get("Text")
            if not isinstance(text, str):
                raise ValueError("Payload.Text must be a string.")
            interval_ms = as_int(payload_get(payload, "IntervalMs", default=0))
        except (TypeError, ValueError) as e:
            return TypeText_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        interval = (interval_ms / 1000.0) if interval_ms > 0 else 0.02
        try:
            self._exec.run(self._exec.gpc.type_text, text, interval)
        except Exception as e:
            return TypeText_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            keys = parse_string_list(payload_get(payload, "Keys"), field="Keys")
            if not keys:
                raise ValueError("Payload.Keys must be a non-empty list of strings.")
        except ValueError as e:
            return Hotkey_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.hotkey, *keys)
        except Exception as e:
            return Hotkey_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            kwargs = parse_locator(payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
        except ValueError as e:
            return FindControl_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            kwargs = parse_locator(payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
        except ValueError as e:
            return ClickControlByLocator_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def find_and_click() -> None:
            ctrl = self._exec.gpc.find_control(**kwargs).wrapper_object()
            self._exec.gpc.click_control(ctrl)

        try:
            self._exec.run(find_and_click)
        except Exception as e:
            return ClickControlByLocator_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            kwargs = parse_locator(payload_get(payload, "Locator", "LocatorJson"))
            if not kwargs:
                raise ValueError(
                    "Locator must include at least one of: title, auto_id, control_type."
                )
            text = payload.get("Text")
            if not isinstance(text, str):
                raise ValueError("Payload.Text must be a string.")
            clear_first = as_bool(payload_get(payload, "ClearFirst", default=False))
        except ValueError as e:
            return TypeIntoControlByLocator_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )

        def find_and_type() -> None:
            ctrl = self._exec.gpc.find_control(**kwargs).wrapper_object()
            self._exec.gpc.type_into_control(ctrl, text, clear_first=clear_first)

        try:
            self._exec.run(find_and_type)
        except Exception as e:
            return TypeIntoControlByLocator_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            title = require_str(payload_dict(req.payload), "Title")
        except ValueError as e:
            return WaitForWindow_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(
                self._exec.gpc.wait_for_window, title, req.timeout_seconds
            )
        except Exception as e:
            return WaitForWindow_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            title = require_str(payload_dict(req.payload), "Title")
        except ValueError as e:
            return WaitUntilGone_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        timeout = req.timeout_seconds if req.timeout_seconds is not None else 10
        try:
            self._exec.run(self._exec.gpc.wait_until_gone, title, timeout)
        except Exception as e:
            return WaitUntilGone_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            depth_raw = as_int(payload_get(payload, "Depth", default=0))
        except (TypeError, ValueError) as e:
            return PrintControlTree_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
            )
        depth = depth_raw if depth_raw > 0 else 3
        try:
            self._exec.run(self._exec.gpc.print_control_tree, None, depth)
        except Exception as e:
            return PrintControlTree_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
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
            payload = payload_dict(req.payload)
            kwargs = parse_locator(payload_get(payload, "Locator", "LocatorJson"))
        except ValueError as e:
            return ListChildren_Responses(
                Result=rejected_payload(str(e), correlation_id=req.correlation_id)
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
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        return ListChildren_Responses(
            Result=succeeded(
                payload=result_payload,
                correlation_id=req.correlation_id,
            )
        )
