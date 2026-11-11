"""InstrumentStatus SiLA feature implementation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sila2.server import MetadataDict, SilaServer

from ..command import CONNECTED_STATES, handle_command, require_str
from ..executor import GpcExecutor
from ..gpc_sila.instrumentstatus.instrumentstatus_base import InstrumentStatusBase
from ..gpc_sila.instrumentstatus.instrumentstatus_types import (
    GetAnalysisStatus_Responses,
    GetColor_Responses,
    GetCurrentSequenceStep_Responses,
    GetInstrumentSnapshot_Responses,
    GetSequenceSummary_Responses,
    GetStartButtonState_Responses,
    GetSystemInfo_Responses,
    GetSystemStatus_Responses,
    GetText_Responses,
    InvalidateCache_Responses,
)
from ..state_machine import GpcStateMachine


class InstrumentStatusImpl(InstrumentStatusBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm
        self.update_CurrentSystemStatus("unknown")
        self.update_CurrentAnalysisStatus("unknown")

    def GetColor(self, Request: Any, *, metadata: MetadataDict) -> GetColor_Responses:
        def work(payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            auto_id = require_str(payload, "AutoId")
            color = self._exec.run(self._exec.gpc.get_color, auto_id)
            return {"color": color}

        return GetColor_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetText(self, Request: Any, *, metadata: MetadataDict) -> GetText_Responses:
        def work(payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            auto_id = require_str(payload, "AutoId")
            text = self._exec.run(self._exec.gpc.get_text_in_settings_window, auto_id)
            return {"text": text}

        return GetText_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetSystemStatus(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSystemStatus_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            color = self._exec.run(self._exec.gpc.get_system_status)
            self.update_CurrentSystemStatus(color)
            return {"color": color}

        return GetSystemStatus_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetAnalysisStatus(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetAnalysisStatus_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            color = self._exec.run(self._exec.gpc.get_analysis_status)
            self.update_CurrentAnalysisStatus(color)
            return {"color": color}

        return GetAnalysisStatus_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetStartButtonState(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetStartButtonState_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
            return {"IsOn": bool(is_on)}

        return GetStartButtonState_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetSequenceSummary(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSequenceSummary_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            summary = self._exec.run(self._exec.gpc.get_sequence_summary)
            return asdict(summary)

        return GetSequenceSummary_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetCurrentSequenceStep(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetCurrentSequenceStep_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            step = self._exec.run(self._exec.gpc.get_current_sequence_step)
            return asdict(step)

        return GetCurrentSequenceStep_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetSystemInfo(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetSystemInfo_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> list[dict[str, Any]]:
            modules = self._exec.run(self._exec.gpc.get_system_info)
            return [asdict(m) for m in modules]

        return GetSystemInfo_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def GetInstrumentSnapshot(
        self, Request: Any, *, metadata: MetadataDict
    ) -> GetInstrumentSnapshot_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            snapshot = self._exec.run(self._exec.gpc.get_instrument_snapshot)
            self.update_CurrentSystemStatus(snapshot.system_status)
            self.update_CurrentAnalysisStatus(snapshot.analysis_status)
            return {
                "system_status": snapshot.system_status,
                "analysis_status": snapshot.analysis_status,
                "sequence_summary": asdict(snapshot.sequence_summary),
                "current_step": asdict(snapshot.current_step),
                "system_info": [asdict(m) for m in snapshot.system_info],
            }

        return GetInstrumentSnapshot_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )

    def InvalidateCache(
        self, Request: Any, *, metadata: MetadataDict
    ) -> InvalidateCache_Responses:
        def work(_payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            self._exec.run(self._exec.gpc.invalidate_cache)
            return {"status": "ok"}

        return InvalidateCache_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )
