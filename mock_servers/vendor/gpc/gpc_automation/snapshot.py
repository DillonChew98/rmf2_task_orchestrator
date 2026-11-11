"""Batched instrument snapshot (single foreground)."""

from __future__ import annotations

import logging
from typing import TypeVar

from gpc_automation.leds import UNKNOWN_COLOR
from gpc_automation.models import (
    CurrentSequenceStep,
    InstrumentSnapshot,
    SequenceSummary,
)

log = logging.getLogger(__name__)

T = TypeVar("T")


class SnapshotMixin:
    """Compose a full instrument snapshot with one bring_to_front."""

    def _safe(self, label: str, fn, default: T) -> T:
        """Call ``fn`` and return its result, logging and returning ``default`` on failure."""
        try:
            return fn()
        except Exception:
            log.error("Snapshot: failed to read %s", label, exc_info=True)
            return default

    def get_instrument_snapshot(self) -> InstrumentSnapshot:
        """
        Read all instrument state in a single call.

        Brings the window forward once, then reads subsystems with
        ``foreground=False`` to avoid redundant focus/sleep cycles.
        """
        self._ensure_connected()
        self.bring_to_front()

        system_status = self._safe(
            "system LED",
            lambda: self.get_system_status(foreground=False),
            UNKNOWN_COLOR,
        )
        analysis_status = self._safe(
            "analysis LED",
            lambda: self.get_analysis_status(foreground=False),
            UNKNOWN_COLOR,
        )
        self._safe("start button cache", self._warm_start_button_cache, None)
        summary = self._safe(
            "sequence summary",
            lambda: self.get_sequence_summary(foreground=False),
            SequenceSummary(),
        )
        current_step = self._safe(
            "current step",
            lambda: self.get_current_sequence_step(foreground=False),
            CurrentSequenceStep(),
        )
        system_info = self._safe(
            "system info",
            lambda: self.get_system_info(foreground=False),
            [],
        )

        snapshot = InstrumentSnapshot(
            system_status=system_status,
            analysis_status=analysis_status,
            sequence_summary=summary,
            current_step=current_step,
            system_info=system_info,
        )
        log.info(
            "Snapshot complete: sys=%s, analysis=%s",
            system_status,
            analysis_status,
        )
        return snapshot
