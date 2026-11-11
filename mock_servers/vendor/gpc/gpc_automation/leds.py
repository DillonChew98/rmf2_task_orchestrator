"""LED pixel capture and color classification."""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

from PIL import ImageGrab
from pywinauto.controls.uiawrapper import UIAWrapper

log = logging.getLogger(__name__)

UNKNOWN_COLOR = "unknown"

LED_COLOR_MAP = [
    ((0, 200, 0), 80, "green"),
    ((0, 128, 0), 60, "green"),
    ((255, 255, 0), 80, "yellow"),
    ((200, 200, 0), 80, "yellow"),
    ((255, 0, 0), 80, "red"),
    ((200, 0, 0), 80, "red"),
    ((128, 128, 128), 60, "gray"),
    ((192, 192, 192), 60, "gray"),
    ((160, 160, 160), 60, "gray"),
    ((0, 0, 255), 80, "blue"),
    ((255, 165, 0), 80, "orange"),
]


class LedsMixin:
    """Read WinGPC status LEDs via screen capture."""

    LED_COLOR_MAP = LED_COLOR_MAP

    def _rgb_distance(self, c1: tuple, c2: tuple) -> float:
        return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) ** 0.5

    def _classify_color(self, rgb: tuple) -> str:
        """Map an RGB tuple to a named LED color."""
        for ref_rgb, tol, label in self.LED_COLOR_MAP:
            if self._rgb_distance(rgb, ref_rgb) <= tol:
                return label
        r, g, b = rgb
        if r > 180 and g > 180 and b > 180:
            return "white"
        if r < 60 and g < 60 and b < 60:
            return "black/off"
        return f"unknown(r={r},g={g},b={b})"

    def _get_control_pixel_color(self, ctrl: UIAWrapper) -> tuple:
        """Capture the dominant color from a small UIA control (like an LED)."""
        rect = ctrl.rectangle()
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        pixels = list(img.getdata())

        core_pixels = [
            p[:3]
            for p in pixels
            if not (p[0] > 230 and p[1] > 230 and p[2] > 230)
            and not (p[0] < 20 and p[1] < 20 and p[2] < 20)
        ]
        if not core_pixels:
            core_pixels = [p[:3] for p in pixels]

        quantized = [
            (r // 16 * 16, g // 16 * 16, b // 16 * 16) for r, g, b in core_pixels
        ]
        result = Counter(quantized).most_common(1)[0][0]
        log.debug("Pixel color capture: %s (%d pixels)", result, len(pixels))
        return result

    def _get_led_color(
        self,
        auto_id: str,
        parent: Optional[UIAWrapper] = None,
        *,
        focus: bool = True,
    ) -> str:
        """Resolve an LED Pane by AutomationId and classify its dominant color."""
        parent = parent or self.get_sequence_window()
        led = self._cached_element(
            f"led_{auto_id}",
            parent,
            auto_id=auto_id,
            control_type="Pane",
        )
        if focus:
            led.set_focus()
            time.sleep(0.1)
        rgb = self._get_control_pixel_color(led)
        return self._classify_color(rgb)

    def get_color(
        self,
        auto_id: str,
        parent: Optional[UIAWrapper] = None,
        *,
        foreground: bool = True,
    ) -> str:
        """
        Read the LED color from a Pane control by its AutomationId.

        When *foreground* is False, skip bring_to_front / LED focus (caller
        already foregrounded the window, e.g. during a batched snapshot).
        """
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        color = self._get_led_color(auto_id, parent, focus=foreground)
        log.debug("LED %s = %s", auto_id, color)
        return color

    def get_system_status(self, *, foreground: bool = True) -> str:
        """Read the system status LED color (AutomationId: ledSystemState)."""
        return self.get_color("ledSystemState", foreground=foreground)

    def get_analysis_status(self, *, foreground: bool = True) -> str:
        """Read the analysis LED color (AutomationId: ledAnalysis)."""
        return self.get_color("ledAnalysis", foreground=foreground)
