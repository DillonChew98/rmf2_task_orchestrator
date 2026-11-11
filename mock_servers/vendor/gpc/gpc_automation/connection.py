"""WinGPC connection, window accessors, and UIA element cache."""

from __future__ import annotations

import logging
import time
from typing import Optional

from pywinauto import Application
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.controls.uiawrapper import UIAWrapper

log = logging.getLogger(__name__)

MAIN_WINDOW_TITLE_RE = r"Agilent WinGPC.*Instance #\d+"
SEQUENCE_AUTO_ID = "SequenceUI"
SETTINGS_AUTO_ID = "MethodUI"


class ConnectionMixin:
    """Connection state and cached child-window accessors."""

    timeout: int
    app: Optional[Application]
    main_window: Optional[UIAWrapper]
    _cache: dict[str, UIAWrapper]

    def connect(self) -> None:
        """Connect to a running WinGPC instance via UIA backend."""
        log.info("Connecting to Agilent WinGPC …")
        self._cache.clear()
        try:
            self.app = Application(backend="uia").connect(
                title_re=MAIN_WINDOW_TITLE_RE,
                timeout=self.timeout,
            )
            self.main_window = self.app.window(title_re=MAIN_WINDOW_TITLE_RE)
            self.main_window.wait("visible", timeout=self.timeout)
            log.info("Connected: %s", self.main_window.window_text())
            try:
                self._warm_start_button_cache()
            except Exception:
                log.debug(
                    "Start-button cache warm failed — will resolve on first use",
                    exc_info=True,
                )
        except ElementNotFoundError:
            log.error("WinGPC window not found. Is the application running?")
            raise

    def bring_to_front(self) -> None:
        """Bring the main WinGPC window to the foreground."""
        self._ensure_connected()
        self.main_window.set_focus()
        time.sleep(0.05)

    def invalidate_cache(self) -> None:
        """Clear cached child-window references (e.g. after UI restructure)."""
        self._cache.clear()
        log.info("Child-window cache cleared")

    def _ensure_connected(self) -> None:
        if self.main_window is None:
            raise RuntimeError("Not connected. Call connect() first.")

    def _cached_child(self, key: str, parent, **kwargs):
        """Return a child window spec, caching for repeated access."""
        if key not in self._cache:
            self._cache[key] = parent.child_window(**kwargs)
        return self._cache[key]

    def _cached_element(self, key: str, parent, **kwargs) -> UIAWrapper:
        """Return a resolved element handle, caching for repeated access."""
        if key not in self._cache:
            spec = parent.child_window(**kwargs)
            self._cache[key] = spec.wrapper_object()
        return self._cache[key]

    def _get_tab_window(self, cache_key: str, auto_id: str, open_fn) -> UIAWrapper:
        """Return a cached child window, opening its tab if not yet visible."""
        spec = self._cached_child(
            cache_key, self.main_window, auto_id=auto_id, control_type="Window"
        )
        try:
            spec.wait("exists", timeout=2)
        except Exception:
            log.info("%s window not found — clicking tab to open it", cache_key)
            open_fn()
            self._cache.pop(cache_key, None)
            spec = self._cached_child(
                cache_key, self.main_window, auto_id=auto_id, control_type="Window"
            )
            spec.wait("exists", timeout=5)
        return spec

    def get_sequence_window(self) -> UIAWrapper:
        """Return the Sequence [DEFAULT] child window, opening the tab if needed."""
        self._ensure_connected()
        return self._get_tab_window("sequence", SEQUENCE_AUTO_ID, self._open_sequence_tab)

    def get_settings_window(self) -> UIAWrapper:
        """Return the Settings [DEFAULT] child window, opening the tab if needed."""
        self._ensure_connected()
        return self._get_tab_window("settings", SETTINGS_AUTO_ID, self._open_settings_tab)

    def get_workspace_pane(self) -> UIAWrapper:
        """Return the Workspace pane."""
        self._ensure_connected()
        return self._cached_child(
            "workspace",
            self.main_window,
            title="Workspace",
            control_type="Pane",
        )

    def get_menu_bar(self) -> UIAWrapper:
        """Return the Application menu bar."""
        self._ensure_connected()
        return self._cached_child(
            "menubar",
            self.main_window,
            title="Application",
            control_type="MenuBar",
        )
