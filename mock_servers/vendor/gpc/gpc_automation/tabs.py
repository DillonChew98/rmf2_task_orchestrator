"""Sequence / Settings tab switching (UIA first, pixel-coords fallback)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SEQUENCE_TAB_CLICK_COORDS = (128, 35)  # relative to InfoWClass pane top-left
SETTINGS_TAB_CLICK_COORDS = (180, 27)


class TabsMixin:
    """Open Sequence / Settings tabs via UIA TabItem when possible."""

    def _open_sequence_tab(self) -> None:
        if self._try_select_tab("Sequence"):
            return
        self._click_tab(SEQUENCE_TAB_CLICK_COORDS, "sequence")

    def _open_settings_tab(self) -> None:
        if self._try_select_tab("Settings"):
            return
        self._click_tab(SETTINGS_TAB_CLICK_COORDS, "settings")

    def _try_select_tab(self, name_substring: str) -> bool:
        """Select a tab by partial title under the main window. Returns True on success."""
        self._ensure_connected()
        try:
            tab = self.main_window.child_window(
                title_re=f".*{name_substring}.*",
                control_type="TabItem",
            )
            tab.wait("exists", timeout=2)
            tab.select()
            log.info("Selected tab via UIA: %s", name_substring)
            return True
        except Exception:
            log.debug(
                "UIA tab select failed for %s — falling back to pixel click",
                name_substring,
                exc_info=True,
            )
            return False

    def _click_tab(self, coords: tuple, name: str) -> None:
        """Click the InfoWClass pane at the given coords to switch tabs."""
        self._ensure_connected()
        self.bring_to_front()
        pane = self._cached_child(
            "info_pane",
            self.main_window,
            class_name="InfoWClass",
            control_type="Pane",
            found_index=7,
        )
        pane.click_input(coords=coords)
        log.info("Clicked InfoWClass pane (%s tab)", name)
