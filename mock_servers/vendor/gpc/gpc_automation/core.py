"""Compatibility re-export — prefer ``gpc_automation.facade`` for new code."""

from gpc_automation.facade import GpcAutomation, get_gpc

__all__ = ["GpcAutomation", "get_gpc"]
