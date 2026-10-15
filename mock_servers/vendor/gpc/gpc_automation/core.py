"""
Agilent WinGPC GUI Automation – Core class

Automates the Agilent WinGPC application using pywinauto (UIA backend)
for reliable WinForms control access, with pyautogui as fallback for
mouse/keyboard operations.

Application details (from Inspect.exe):
  - Main window: "Agilent WinGPC , Build 11494, IMR-SPD-0300872, Instance #1"
  - FrameworkId: WinForm
  - ClassName: WindowsForms10.Window.8.app.0.13965fa_r7_ad1
  - Key child windows:
      - "Instrument 1 (GPC): Sequence [DEFAULT]" (AutomationId: SequenceUI)
      - "Instrument 1 (GPC): Settings [DEFAULT]" (AutomationId: MethodUI)
      - "Workspace" pane
      - "Application" menu bar
"""

import time
import logging
from typing import Optional, TypeVar

from gpc_automation.models import (
    CurrentSequenceStep,
    InstrumentSnapshot,
    SequenceSummary,
    InstrumentInfo,
)

T = TypeVar("T")
from collections import Counter

import pyautogui
from PIL import ImageGrab
from pywinauto import Application, Desktop, timings
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.controls.uiawrapper import UIAWrapper

log = logging.getLogger(__name__)

# Match the title pattern from Inspect.exe
MAIN_WINDOW_TITLE_RE = r"Agilent WinGPC.*Instance #\d+"
SEQUENCE_AUTO_ID = "SequenceUI"
SETTINGS_AUTO_ID = "MethodUI"
SEQUENCE_TAB_CLICK_COORDS = (128, 35)   # relative to InfoWClass pane top-left
SETTINGS_TAB_CLICK_COORDS = (180, 27)   # relative to InfoWClass pane top-left

# Sentinel returned by snapshot when an LED read fails
UNKNOWN_COLOR = "unknown"

# pyautogui safety settings
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


class GpcAutomation:
    """High-level automation interface for Agilent WinGPC."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.app: Optional[Application] = None
        self.main_window: Optional[UIAWrapper] = None
        # Cached child-window references (cleared on reconnect)
        self._cache: dict[str, UIAWrapper] = {}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self):
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
            saved_timeout = self.timeout
            self.timeout = max(self.timeout, 60)
            try:
                self.get_instrument_snapshot()
            except Exception:
                log.error(
                    "Initial instrument snapshot failed — some controls may not be available yet",
                    exc_info=True,
                )
            finally:
                self.timeout = saved_timeout
        except ElementNotFoundError:
            log.error("WinGPC window not found. Is the application running?")
            raise

    def bring_to_front(self):
        """Bring the main WinGPC window to the foreground."""
        self._ensure_connected()
        self.main_window.set_focus()
        time.sleep(0.05)

    # ------------------------------------------------------------------
    # Child-window accessors
    # ------------------------------------------------------------------

    def _cached_child(self, key: str, parent, **kwargs):
        """Return a child window spec, caching for repeated access."""
        if key not in self._cache:
            self._cache[key] = parent.child_window(**kwargs)
        return self._cache[key]

    def _cached_element(self, key: str, parent, **kwargs) -> UIAWrapper:
        """Return a resolved element handle, caching for repeated access. Use for leaf controls only."""
        if key not in self._cache:
            spec = parent.child_window(**kwargs)
            self._cache[key] = spec.wrapper_object()
        return self._cache[key]

    def _get_tab_window(self, cache_key: str, auto_id: str, open_fn) -> UIAWrapper:
        """Return a cached child window, clicking its tab to open it if not yet visible."""
        spec = self._cached_child(cache_key, self.main_window, auto_id=auto_id, control_type="Window")
        try:
            spec.wait("exists", timeout=2)
        except Exception:
            log.info("%s window not found — clicking tab to open it", cache_key)
            open_fn()
            self._cache.pop(cache_key, None)
            spec = self._cached_child(cache_key, self.main_window, auto_id=auto_id, control_type="Window")
            spec.wait("exists", timeout=5)
        return spec

    def get_sequence_window(self) -> UIAWrapper:
        """Return the Sequence [DEFAULT] child window, opening the tab if not visible."""
        self._ensure_connected()
        return self._get_tab_window("sequence", SEQUENCE_AUTO_ID, self._open_sequence_tab)

    def get_settings_window(self) -> UIAWrapper:
        """Return the Settings [DEFAULT] child window, opening the tab if not visible."""
        self._ensure_connected()
        return self._get_tab_window("settings", SETTINGS_AUTO_ID, self._open_settings_tab)

    def get_workspace_pane(self) -> UIAWrapper:
        """Return the Workspace pane."""
        self._ensure_connected()
        return self._cached_child(
            "workspace", self.main_window,
            title="Workspace", control_type="Pane",
        )

    def get_menu_bar(self) -> UIAWrapper:
        """Return the Application menu bar."""
        self._ensure_connected()
        return self._cached_child(
            "menubar", self.main_window,
            title="Application", control_type="MenuBar",
        )

    # ------------------------------------------------------------------
    # LED status detection
    # ------------------------------------------------------------------

    LED_COLOR_MAP = [
        # (R, G, B), tolerance, label
        ((0, 200, 0),     80, "green"),
        ((0, 128, 0),     60, "green"),
        ((255, 255, 0),   80, "yellow"),
        ((200, 200, 0),   80, "yellow"),
        ((255, 0, 0),     80, "red"),
        ((200, 0, 0),     80, "red"),
        ((128, 128, 128), 60, "gray"),
        ((192, 192, 192), 60, "gray"),
        ((160, 160, 160), 60, "gray"),
        ((0, 0, 255),     80, "blue"),
        ((255, 165, 0),   80, "orange"),
    ]

    def _rgb_distance(self, c1: tuple, c2: tuple) -> float:
        return ((c1[0]-c2[0])**2 + (c1[1]-c2[1])**2 + (c1[2]-c2[2])**2) ** 0.5

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
            p[:3] for p in pixels
            if not (p[0] > 230 and p[1] > 230 and p[2] > 230)
            and not (p[0] < 20 and p[1] < 20 and p[2] < 20)
        ]
        if not core_pixels:
            core_pixels = [p[:3] for p in pixels]

        quantized = [
            (r // 16 * 16, g // 16 * 16, b // 16 * 16)
            for r, g, b in core_pixels
        ]
        result = Counter(quantized).most_common(1)[0][0]
        log.debug("Pixel color capture: %s (%d pixels)", result, len(pixels))
        return result

    def _get_led_color(self, auto_id: str, parent: Optional[UIAWrapper] = None) -> str:
        """Resolve an LED Pane by AutomationId and classify its dominant color."""
        parent = parent or self.get_sequence_window()
        led = self._cached_element(
            f"led_{auto_id}", parent,
            auto_id=auto_id, control_type="Pane",
        )
        led.set_focus()
        time.sleep(0.1)
        rgb = self._get_control_pixel_color(led)
        return self._classify_color(rgb)

    def get_color(self, auto_id: str, parent: Optional[UIAWrapper] = None) -> str:
        """
        Read the LED color from a Pane control by its AutomationId.

        Args:
            auto_id: The AutomationId of the pane control.
            parent: Parent wrapper to search under (defaults to Sequence window).

        Returns a color string: "green", "yellow", "red", "gray", etc.
        """
        self._ensure_connected()
        self.bring_to_front()
        color = self._get_led_color(auto_id, parent)
        log.debug("LED %s = %s", auto_id, color)
        return color

    def get_text_in_settings_window(self, auto_id: str, parent: Optional[UIAWrapper] = None) -> str:
        """
        Read the text value from a Text control by its AutomationId.

        Args:
            auto_id: The AutomationId of the text control.
            parent: Parent wrapper to search under (defaults to Settings window).

        Returns the raw text string from the control.
        """
        self._ensure_connected()
        self.bring_to_front()
        parent = parent or self.get_settings_window()
        label = self._cached_element(
            f"text_{auto_id}", parent,
            auto_id=auto_id, control_type="Text",
        )
        label.set_focus()
        raw = label.window_text()
        log.info("Text %s = %r", auto_id, raw)
        return raw

    def get_system_status(self) -> str:
        """Read the system status LED color (AutomationId: ledSystemState)."""
        return self.get_color("ledSystemState")

    def get_analysis_status(self) -> str:
        """Read the analysis LED color (AutomationId: ledAnalysis)."""
        return self.get_color("ledAnalysis")

    # ------------------------------------------------------------------
    # Sequence start/stop control
    # ------------------------------------------------------------------

    def click_start(self):
        """
        Click the Start button in the Sequence window.

        Despite appearing as a button, this is a CheckBox control
        (AutomationId: checkBoxButtonStart) with Invoke and Toggle patterns.
        """
        self._ensure_connected()
        self.bring_to_front()

        seq = self.get_sequence_window()
        start_btn = self._cached_element(
            "checkBoxButtonStart", seq,
            auto_id="checkBoxButtonStart", control_type="CheckBox",
        )
        start_btn.invoke()
        log.info("Clicked Start (checkBoxButtonStart)")

    def get_start_button_state(self) -> bool:
        """Return True if the Start checkbox-button is currently toggled on."""
        self._ensure_connected()
        self.bring_to_front()
        seq = self.get_sequence_window()
        start_btn = self._cached_element(
            "checkBoxButtonStart", seq,
            auto_id="checkBoxButtonStart", control_type="CheckBox",
        )
        start_btn.set_focus()
        state = start_btn.get_toggle_state()
        is_on = state == 1
        log.info("Start button state: %s (%s)", state, "ON" if is_on else "OFF")
        return is_on

    # ------------------------------------------------------------------
    # Instrument readings
    # ------------------------------------------------------------------

    def _read_text_by_id(self, parent, auto_id: str) -> str:
        """Read window_text() from a cached Text element by AutomationId."""
        ctrl = self._cached_element(
            f"text_{auto_id}", parent,
            auto_id=auto_id, control_type="Text",
        )
        return ctrl.window_text()

    def get_sequence_summary(self) -> SequenceSummary:
        """
        Read the Sequence Summary from the Sequence window via cached element lookups.
        """
        self._ensure_connected()
        self.bring_to_front()
        seq = self.get_sequence_window()
        seq.set_focus()

        summary = SequenceSummary(
            injections_elapsed=self._read_text_by_id(seq, "labelInjectionsElapsedValue"),
            injections_left=self._read_text_by_id(seq, "labelInjectionsLeft"),
            time_elapsed=self._read_text_by_id(seq, "labelTimeElapsedValue"),
            time_left=self._read_text_by_id(seq, "labelTimeLeftValue"),
            solvent_elapsed=self._read_text_by_id(seq, "labelSolventElapsedValue"),
            solvent_left=self._read_text_by_id(seq, "labelSolventLeftValue"),
        )

        log.info(
            "Sequence summary: inj=%s/%s, time=%s/%s",
            summary.injections_elapsed, summary.injections_left,
            summary.time_elapsed, summary.time_left,
        )
        return summary

    def get_current_sequence_step(self) -> CurrentSequenceStep:
        """
        Read the "Current sequence step" from the Sequence window via cached element lookups.
        """
        self._ensure_connected()
        self.bring_to_front()
        seq = self.get_sequence_window()
        seq.set_focus()

        result = CurrentSequenceStep(
            sequence_step=self._read_text_by_id(seq, "labelSampleNameValue"),
            position=self._read_text_by_id(seq, "labelPositionValue"),
            injection_time=self._read_text_by_id(seq, "labelInjectionTimeValueSystem"),
        )

        log.info(
            "Current step: step=%s, pos=%s, inj_time=%s",
            result.sequence_step, result.position, result.injection_time,
        )
        return result

    def get_system_info(self) -> list[InstrumentInfo]:
        """
        Read labelDeviceCaption, ledState, and ledAnalysis for every instrument module.

        Iterates found_index=0,1,2,… until labelDeviceCaption is no longer found,
        collecting one InstrumentInfo per module.
        """
        self._ensure_connected()
        self.bring_to_front()
        inst_win = self.get_settings_window()
        inst_win.set_focus()
        results = []
        idx = 0
        while True:
            try:
                title_ctrl = self._cached_element(
                    f"sys_device_caption_{idx}", inst_win,
                    auto_id="labelDeviceCaption", control_type="Text", found_index=idx,
                )
                device_title = title_ctrl.window_text()
            except Exception:
                break  # no more modules

            if device_title.lower() == "system settings":
                idx += 1
                continue

            state_color = UNKNOWN_COLOR
            analysis_color = UNKNOWN_COLOR
            state_argb = ""

            try:
                led = self._cached_element(
                    f"sys_led_state_{idx}", inst_win,
                    auto_id="ledState", control_type="Pane", found_index=idx,
                )
                rgb = self._get_control_pixel_color(led)
                state_color = self._classify_color(rgb)
                r, g, b = rgb
                state_argb = f"#{255:02X}{r:02X}{g:02X}{b:02X}"
            except Exception:
                log.debug("Could not read ledState[%d]", idx, exc_info=True)

            try:
                led = self._cached_element(
                    f"sys_led_analysis_{idx}", inst_win,
                    auto_id="ledAnalysis", control_type="Pane", found_index=idx,
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

    # -- Instrument snapshot (sequential, with per-subsystem error handling) --

    def _safe(self, label: str, fn, default: T) -> T:
        """Call ``fn`` and return its result, logging and returning ``default`` on failure."""
        try:
            return fn()
        except Exception:
            log.error("Snapshot: failed to read %s", label, exc_info=True)
            return default

    def _warm_start_button_cache(self) -> None:
        """Resolve the Start checkbox-button so subsequent clicks/state reads are cached."""
        seq = self.get_sequence_window()
        self._cached_element(
            "checkBoxButtonStart", seq,
            auto_id="checkBoxButtonStart", control_type="CheckBox",
        )

    def get_instrument_snapshot(self) -> InstrumentSnapshot:
        """
        Read all instrument state in a single call.

        Each subsystem is wrapped via ``_safe`` — failures are logged and
        substituted with empty defaults so one broken control doesn't block
        the entire snapshot.
        """
        self._ensure_connected()
        self.bring_to_front()

        system_status = self._safe("system LED", self.get_system_status, UNKNOWN_COLOR)
        analysis_status = self._safe("analysis LED", self.get_analysis_status, UNKNOWN_COLOR)
        self._safe("start button cache", self._warm_start_button_cache, None)
        summary = self._safe(
            "sequence summary", self.get_sequence_summary, SequenceSummary(),
        )
        current_step = self._safe(
            "current step", self.get_current_sequence_step, CurrentSequenceStep(),
        )
        system_info = self._safe(
            "system info", self.get_system_info, [],
        )

        snapshot = InstrumentSnapshot(
            system_status=system_status,
            analysis_status=analysis_status,
            sequence_summary=summary,
            current_step=current_step,
            system_info=system_info,
        )
        log.info("Snapshot complete: sys=%s, analysis=%s", system_status, analysis_status)
        return snapshot

    def invalidate_cache(self):
        """Clear cached child-window references (e.g. after UI restructure)."""
        self._cache.clear()
        log.info("Child-window cache cleared")

    # ------------------------------------------------------------------
    # Menu operations
    # ------------------------------------------------------------------

    def click_menu(self, *menu_path: str):
        """
        Navigate the Application menu bar by item names.

        Example:
            gpc.click_menu("File", "Open")
        """
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

    # ------------------------------------------------------------------
    # Control interaction helpers
    # ------------------------------------------------------------------

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

    def click_control(self, ctrl: UIAWrapper):
        """Click a UIA control (brings window to front first)."""
        self.bring_to_front()
        ctrl.click_input()
        log.info("Clicked control: %s", ctrl.window_text() or ctrl.automation_id())

    def type_into_control(self, ctrl: UIAWrapper, text: str, clear_first: bool = True):
        """Type text into an editable UIA control."""
        self.bring_to_front()
        ctrl.set_focus()
        if clear_first:
            ctrl.set_edit_text("")
        ctrl.type_keys(text, with_spaces=True)
        log.info("Typed into %s (%d chars)", ctrl.automation_id() or ctrl.window_text(), len(text))

    def _click_tab(self, coords: tuple, name: str) -> None:
        """Click the InfoWClass pane (Win32, found_index=7) at the given coords to switch tabs."""
        self._ensure_connected()
        self.bring_to_front()
        pane = self._cached_child(
            "info_pane", self.main_window,
            class_name="InfoWClass", control_type="Pane", found_index=7,
        )
        pane.click_input(coords=coords)
        log.info("Clicked InfoWClass pane (%s tab)", name)

    def _open_sequence_tab(self):
        self._click_tab(SEQUENCE_TAB_CLICK_COORDS, "sequence")

    def _open_settings_tab(self):
        self._click_tab(SETTINGS_TAB_CLICK_COORDS, "settings")

    def _click_load_sequence(self) -> None:
        """Click the Load toolbar button in the Sequence window."""
        self._ensure_connected()
        self.bring_to_front()
        log.info("_click_load_sequence: getting sequence window")
        seq = self.get_sequence_window()
        log.info("_click_load_sequence: finding toolStripContainer1")
        toolbar = self._cached_child(
            "seq_toolbar", seq,
            auto_id="toolStripContainer1", control_type="Pane",
        )
        log.info("_click_load_sequence: finding Import table button")
        btn = self._cached_child(
            "btn_load_sequence", toolbar,
            title="Import table", control_type="Button",
        )
        log.info("_click_load_sequence: waiting for button to exist")
        seq.set_focus()
        btn.wait("exists", timeout=self.timeout)
        log.info("_click_load_sequence: clicking button")
        btn.click_input()
        log.info("Clicked Load sequence toolbar button")

    def load_sequence_file(self, directory: str, filename: str, dialog_title: str = "Open") -> None:
        """Load a WinGPC method file via the Load toolbar button and the Open dialog.

        Args:
            directory: Directory containing the file, e.g. r"C:\\WinGPC\\Methods"
            filename: File name, e.g. "my_method.met"
            dialog_title: Title of the Open file dialog (default "Open").
        """
        self._click_load_sequence()

        log.info("load_sequence_file: waiting for '%s' dialog", dialog_title)
        dialog = self.wait_for_window(dialog_title)

        # Click the address bar (AutomationId "1001") to activate its inline edit
        # mode, then type the directory path and press Enter to navigate there.
        log.info("load_sequence_file: finding address bar (auto_id=1001)")
        address_bar = dialog.child_window(auto_id="1001", control_type="ToolBar")
        address_bar.wait("visible", timeout=self.timeout)
        log.info("load_sequence_file: clicking address bar")
        address_bar.click_input()
        time.sleep(0.2)
        log.info("load_sequence_file: typing directory: %s", directory)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite(directory, interval=0.02)
        pyautogui.press("enter")
        log.info("Navigated Open dialog to directory: %s", directory)

    # ------------------------------------------------------------------
    # pyautogui fallback helpers
    # ------------------------------------------------------------------

    def click_at(self, x: int, y: int, clicks: int = 1):
        """Click at absolute screen coordinates using pyautogui."""
        self.bring_to_front()
        pyautogui.click(x, y, clicks=clicks)
        log.info("Click at (%d, %d) x%d", x, y, clicks)

    def type_text(self, text: str, interval: float = 0.02):
        """Type text via pyautogui (works when UIA type_keys fails)."""
        pyautogui.typewrite(text, interval=interval)
        log.info("Typed %d chars via pyautogui", len(text))

    def hotkey(self, *keys: str):
        """Send a hotkey combo via pyautogui. E.g. hotkey('ctrl', 's')."""
        pyautogui.hotkey(*keys)
        log.info("Hotkey: %s", "+".join(keys))

    def screenshot(self, filename: str = "gpc_screenshot.png"):
        """Take a screenshot and save to disk."""
        self._ensure_connected()
        self.bring_to_front()
        img = pyautogui.screenshot()
        img.save(filename)
        log.info("Screenshot saved: %s", filename)

    # ------------------------------------------------------------------
    # Tree exploration / debugging
    # ------------------------------------------------------------------

    def print_control_tree(self, root: Optional[UIAWrapper] = None, depth: int = 3):
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

    # ------------------------------------------------------------------
    # Wait utilities
    # ------------------------------------------------------------------

    def wait_for_window(self, title: str, timeout: Optional[int] = None) -> UIAWrapper:
        """Wait for a window (e.g. a dialog) to appear by title."""
        timeout = timeout or self.timeout
        dlg = Desktop(backend="uia").window(title=title)
        dlg.wait("visible", timeout=timeout)
        log.info("Window appeared: %s", title)
        return dlg

    def wait_until_gone(self, title: str, timeout: int = 10):
        """Wait until a window/dialog with the given title disappears."""
        timings.wait_until(
            timeout=timeout,
            retry_interval=0.5,
            func=lambda: not Desktop(backend="uia")
            .window(title=title)
            .exists(),
        )
        log.info("Window gone: %s", title)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_connected(self):
        if self.main_window is None:
            raise RuntimeError("Not connected. Call connect() first.")


def get_gpc(timeout: int = 10) -> GpcAutomation:
    """Create and connect a GpcAutomation instance."""
    gpc = GpcAutomation(timeout=timeout)
    gpc.connect()
    return gpc

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    gpc = get_gpc()
    gpc.get_system_status()
    gpc.get_analysis_status()
    gpc.get_instrument_snapshot()
