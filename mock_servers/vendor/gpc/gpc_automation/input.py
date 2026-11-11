"""Menu, mouse/keyboard, control find, waits, screenshot."""

from __future__ import annotations

import logging
import time
from typing import Optional

import pyautogui
from pywinauto import Desktop, timings
from pywinauto.controls.uiawrapper import UIAWrapper

log = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


class InputMixin:
    """Low-level UI input and exploration helpers."""

    def click_menu(self, *menu_path: str) -> None:
        """Navigate the Application menu bar by item names."""
        self._ensure_connected()
        self.bring_to_front()
        menu_bar = self.get_menu_bar()

        current = menu_bar
        for item_name in menu_path:
            menu_item = current.child_window(title=item_name, control_type="MenuItem")
            menu_item.wait("visible", timeout=self.timeout)
            menu_item.click_input()
            time.sleep(0.1)
            current = menu_item
            log.info("Clicked menu item: %s", item_name)
        log.info("Menu navigation %s completed", " > ".join(menu_path))

    def find_control(
        self,
        parent: Optional[UIAWrapper] = None,
        *,
        title: Optional[str] = None,
        auto_id: Optional[str] = None,
        control_type: Optional[str] = None,
    ) -> UIAWrapper:
        """Find a control under `parent` (defaults to main window)."""
        self._ensure_connected()
        parent = parent or self.main_window

        kwargs = {}
        if title:
            kwargs["title"] = title
        if auto_id:
            kwargs["auto_id"] = auto_id
        if control_type:
            kwargs["control_type"] = control_type

        ctrl = parent.child_window(**kwargs)
        ctrl.wait("exists", timeout=self.timeout)
        log.info("Found control: title=%s auto_id=%s type=%s", title, auto_id, control_type)
        return ctrl

    def click_control(self, ctrl: UIAWrapper) -> None:
        """Click a UIA control (brings window to front first)."""
        self.bring_to_front()
        ctrl.click_input()
        log.info("Clicked control: %s", ctrl.window_text() or ctrl.automation_id())

    def type_into_control(
        self, ctrl: UIAWrapper, text: str, clear_first: bool = True
    ) -> None:
        """Type text into an editable UIA control."""
        self.bring_to_front()
        ctrl.set_focus()
        if clear_first:
            ctrl.set_edit_text("")
        ctrl.type_keys(text, with_spaces=True)
        log.info(
            "Typed into %s (%d chars)",
            ctrl.automation_id() or ctrl.window_text(),
            len(text),
        )

    def click_at(self, x: int, y: int, clicks: int = 1) -> None:
        """Click at absolute screen coordinates using pyautogui."""
        self.bring_to_front()
        pyautogui.click(x, y, clicks=clicks)
        log.info("Click at (%d, %d) x%d", x, y, clicks)

    def type_text(self, text: str, interval: float = 0.02) -> None:
        """Type text via pyautogui (works when UIA type_keys fails)."""
        pyautogui.typewrite(text, interval=interval)
        log.info("Typed %d chars via pyautogui", len(text))

    def hotkey(self, *keys: str) -> None:
        """Send a hotkey combo via pyautogui. E.g. hotkey('ctrl', 's')."""
        pyautogui.hotkey(*keys)
        log.info("Hotkey: %s", "+".join(keys))

    def screenshot(self, filename: str = "gpc_screenshot.png") -> None:
        """Take a screenshot and save to disk."""
        self._ensure_connected()
        self.bring_to_front()
        img = pyautogui.screenshot()
        img.save(filename)
        log.info("Screenshot saved: %s", filename)

    def print_control_tree(
        self, root: Optional[UIAWrapper] = None, depth: int = 3
    ) -> None:
        """Print the UIA control tree (useful for discovering AutomationIds)."""
        self._ensure_connected()
        root = root or self.main_window
        root.print_control_identifiers(depth=depth)

    def list_children(self, parent: Optional[UIAWrapper] = None):
        """List immediate children of a control with their key UIA properties."""
        self._ensure_connected()
        parent = parent or self.main_window
        children = parent.children()
        for i, child in enumerate(children):
            print(
                f"  [{i}] type={child.element_info.control_type!r}  "
                f"name={child.window_text()!r}  "
                f"auto_id={child.element_info.automation_id!r}"
            )
        return children

    def wait_for_window(self, title: str, timeout: Optional[int] = None) -> UIAWrapper:
        """Wait for a window (e.g. a dialog) to appear by title."""
        timeout = timeout or self.timeout
        dlg = Desktop(backend="uia").window(title=title)
        dlg.wait("visible", timeout=timeout)
        log.info("Window appeared: %s", title)
        return dlg

    def wait_until_gone(self, title: str, timeout: int = 10) -> None:
        """Wait until a window/dialog with the given title disappears."""
        timings.wait_until(
            timeout=timeout,
            retry_interval=0.5,
            func=lambda: not Desktop(backend="uia").window(title=title).exists(),
        )
        log.info("Window gone: %s", title)
