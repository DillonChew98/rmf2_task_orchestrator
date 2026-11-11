"""
Agilent WinGPC GUI Automation – public facade.

Composes connection, LEDs, sequence, settings, snapshot, input, and tabs
mixins into a single ``GpcAutomation`` class.
"""

from __future__ import annotations

import logging
from typing import Optional

from pywinauto import Application
from pywinauto.controls.uiawrapper import UIAWrapper

from gpc_automation.connection import ConnectionMixin
from gpc_automation.input import InputMixin
from gpc_automation.leds import LedsMixin
from gpc_automation.sequence import SequenceMixin
from gpc_automation.settings import SettingsMixin
from gpc_automation.snapshot import SnapshotMixin
from gpc_automation.tabs import TabsMixin

log = logging.getLogger(__name__)


class GpcAutomation(
    ConnectionMixin,
    TabsMixin,
    LedsMixin,
    SequenceMixin,
    SettingsMixin,
    SnapshotMixin,
    InputMixin,
):
    """High-level automation interface for Agilent WinGPC."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.app: Optional[Application] = None
        self.main_window: Optional[UIAWrapper] = None
        self._cache: dict[str, UIAWrapper] = {}


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
