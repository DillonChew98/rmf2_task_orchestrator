"""Settings panel: text reads and system info modules."""

from __future__ import annotations

import logging
from typing import Optional

from pywinauto.controls.uiawrapper import UIAWrapper

from gpc_automation.leds import UNKNOWN_COLOR
from gpc_automation.models import InstrumentInfo

log = logging.getLogger(__name__)


class SettingsMixin:
    """Settings / MethodUI window reads."""

    def get_text_in_settings_window(
        self,
        auto_id: str,
        parent: Optional[UIAWrapper] = None,
        *,
        foreground: bool = True,
    ) -> str:
        """Read the text value from a Text control by its AutomationId."""
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        parent = parent or self.get_settings_window()
        label = self._cached_element(
            f"text_{auto_id}",
            parent,
            auto_id=auto_id,
            control_type="Text",
        )
        if foreground:
            label.set_focus()
        raw = label.window_text()
        log.info("Text %s = %r", auto_id, raw)
        return raw

    def get_system_info(self, *, foreground: bool = True) -> list[InstrumentInfo]:
        """
        Read labelDeviceCaption, ledState, and ledAnalysis for every instrument module.
        """
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        inst_win = self.get_settings_window()
        if foreground:
            inst_win.set_focus()
        results = []
        idx = 0
        while True:
            try:
                title_ctrl = self._cached_element(
                    f"sys_device_caption_{idx}",
                    inst_win,
                    auto_id="labelDeviceCaption",
                    control_type="Text",
                    found_index=idx,
                )
                device_title = title_ctrl.window_text()
            except Exception:
                break

            if device_title.lower() == "system settings":
                idx += 1
                continue

            state_color = UNKNOWN_COLOR
            analysis_color = UNKNOWN_COLOR
            state_argb = ""

            try:
                led = self._cached_element(
                    f"sys_led_state_{idx}",
                    inst_win,
                    auto_id="ledState",
                    control_type="Pane",
                    found_index=idx,
                )
                rgb = self._get_control_pixel_color(led)
                state_color = self._classify_color(rgb)
                r, g, b = rgb
                state_argb = f"#{255:02X}{r:02X}{g:02X}{b:02X}"
            except Exception:
                log.debug("Could not read ledState[%d]", idx, exc_info=True)

            try:
                led = self._cached_element(
                    f"sys_led_analysis_{idx}",
                    inst_win,
                    auto_id="ledAnalysis",
                    control_type="Pane",
                    found_index=idx,
                )
                rgb = self._get_control_pixel_color(led)
                analysis_color = self._classify_color(rgb)
            except Exception:
                log.debug("Could not read ledAnalysis[%d]", idx, exc_info=True)

            prefix = f"{device_title} — " if device_title else ""
            status_text = f"{prefix}state: {state_color}, analysis: {analysis_color}"
            results.append(InstrumentInfo(status_text=status_text, color_argb=state_argb))
            log.info("System info[%d]: %s", idx, status_text)
            idx += 1

        return results
