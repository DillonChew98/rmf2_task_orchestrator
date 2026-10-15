"""Dataclasses for instrument UI state read from WinGPC."""

from dataclasses import dataclass, field


@dataclass
class SequenceSummary:
    injections_elapsed: str = ""
    injections_left: str = ""
    time_elapsed: str = ""
    time_left: str = ""
    solvent_elapsed: str = ""
    solvent_left: str = ""


@dataclass
class CurrentSequenceStep:
    sequence_step: str = ""
    position: str = ""
    injection_time: str = ""


@dataclass
class InstrumentInfo:
    status_text: str = ""
    color_argb: str = ""


@dataclass
class InstrumentSnapshot:
    system_status: str = ""
    analysis_status: str = ""
    sequence_summary: SequenceSummary = field(default_factory=SequenceSummary)
    current_step: CurrentSequenceStep = field(default_factory=CurrentSequenceStep)
    system_info: list[InstrumentInfo] = field(default_factory=list)
