"""Sequence panel: start button, summary, current step."""

from __future__ import annotations

import logging

from gpc_automation.models import CurrentSequenceStep, SequenceSummary

log = logging.getLogger(__name__)


class SequenceMixin:
    """Sequence window reads and Start control."""

    def _read_text_by_id(self, parent, auto_id: str) -> str:
        """Read window_text() from a cached Text element by AutomationId."""
        ctrl = self._cached_element(
            f"text_{auto_id}",
            parent,
            auto_id=auto_id,
            control_type="Text",
        )
        return ctrl.window_text()

    def _warm_start_button_cache(self) -> None:
        """Resolve the Start checkbox-button so subsequent clicks/state reads are cached."""
        seq = self.get_sequence_window()
        self._cached_element(
            "checkBoxButtonStart",
            seq,
            auto_id="checkBoxButtonStart",
            control_type="CheckBox",
        )

    def click_start(self, *, foreground: bool = True) -> None:
        """
        Click the Start button in the Sequence window.

        Despite appearing as a button, this is a CheckBox control
        (AutomationId: checkBoxButtonStart) with Invoke and Toggle patterns.
        """
        self._ensure_connected()
        if foreground:
            self.bring_to_front()

        seq = self.get_sequence_window()
        start_btn = self._cached_element(
            "checkBoxButtonStart",
            seq,
            auto_id="checkBoxButtonStart",
            control_type="CheckBox",
        )
        start_btn.invoke()
        log.info("Clicked Start (checkBoxButtonStart)")

    def get_start_button_state(self, *, foreground: bool = True) -> bool:
        """Return True if the Start checkbox-button is currently toggled on."""
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        seq = self.get_sequence_window()
        start_btn = self._cached_element(
            "checkBoxButtonStart",
            seq,
            auto_id="checkBoxButtonStart",
            control_type="CheckBox",
        )
        start_btn.set_focus()
        state = start_btn.get_toggle_state()
        is_on = state == 1
        log.info("Start button state: %s (%s)", state, "ON" if is_on else "OFF")
        return is_on

    def get_sequence_summary(self, *, foreground: bool = True) -> SequenceSummary:
        """Read the Sequence Summary from the Sequence window."""
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        seq = self.get_sequence_window()
        if foreground:
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
            summary.injections_elapsed,
            summary.injections_left,
            summary.time_elapsed,
            summary.time_left,
        )
        return summary

    def get_current_sequence_step(self, *, foreground: bool = True) -> CurrentSequenceStep:
        """Read the current sequence step from the Sequence window."""
        self._ensure_connected()
        if foreground:
            self.bring_to_front()
        seq = self.get_sequence_window()
        if foreground:
            seq.set_focus()

        result = CurrentSequenceStep(
            sequence_step=self._read_text_by_id(seq, "labelSampleNameValue"),
            position=self._read_text_by_id(seq, "labelPositionValue"),
            injection_time=self._read_text_by_id(seq, "labelInjectionTimeValueSystem"),
        )

        log.info(
            "Current step: step=%s, pos=%s, inj_time=%s",
            result.sequence_step,
            result.position,
            result.injection_time,
        )
        return result
