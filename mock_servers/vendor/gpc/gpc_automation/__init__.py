"""
Agilent WinGPC GUI Automation Package

Usage:
    from gpc_automation import GpcAutomation, get_gpc

    gpc = get_gpc()
    gpc.get_system_status()
    gpc.get_analysis_status()
    gpc.click_start()
"""

from gpc_automation.core import GpcAutomation, get_gpc
from gpc_automation.models import (
    CurrentSequenceStep,
    InstrumentSnapshot,
    SequenceSummary,
    InstrumentInfo,
)

__all__ = [
    "CurrentSequenceStep",
    "GpcAutomation",
    "InstrumentSnapshot",
    "SequenceSummary",
    "InstrumentInfo",
    "get_gpc",
]
