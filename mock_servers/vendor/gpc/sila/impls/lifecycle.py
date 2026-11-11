"""InstrumentLifecycle SiLA feature implementation."""

from __future__ import annotations

import logging
from typing import Any

from sila2.server import MetadataDict, SilaServer
from sila_common.envelope import parse_request, succeeded

from ..command import (
    CONNECTED_STATES,
    failed_automation,
    rejected_state,
)
from ..executor import GpcExecutor
from ..gpc_sila.instrumentlifecycle.instrumentlifecycle_base import (
    InstrumentLifecycleBase,
)
from ..gpc_sila.instrumentlifecycle.instrumentlifecycle_types import (
    BringToFront_Responses,
    Connect_Responses,
    Disconnect_Responses,
    EnterMaintenance_Responses,
    ExitMaintenance_Responses,
    Pause_Responses,
    Resume_Responses,
    Stop_Responses,
)
from ..state_machine import GpcState, GpcStateMachine, InvalidStateTransition

log = logging.getLogger(__name__)


class InstrumentLifecycleImpl(InstrumentLifecycleBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm
        self._fsm.set_on_change(self._publish_state)
        self.update_CurrentState(self._fsm.state.value)

    def _publish_state(self, state: GpcState) -> None:
        self.update_CurrentState(state.value)

    def Connect(self, Request: Any, *, metadata: MetadataDict) -> Connect_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.DISCONNECTED)
        except InvalidStateTransition as e:
            return Connect_Responses(Result=rejected_state(e, correlation_id=req.correlation_id))
        try:
            self._exec.run(self._exec.gpc.connect)
        except Exception as e:
            log.exception("Connect failed")
            return Connect_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.CONNECT)
        return Connect_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Disconnect(self, Request: Any, *, metadata: MetadataDict) -> Disconnect_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.CONNECT)
        except InvalidStateTransition as e:
            return Disconnect_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.invalidate_cache)
            self._exec.gpc.app = None
            self._exec.gpc.main_window = None
        except Exception as e:
            log.exception("Disconnect failed")
            return Disconnect_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.DISCONNECTED)
        return Disconnect_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def BringToFront(self, Request: Any, *, metadata: MetadataDict) -> BringToFront_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(*CONNECTED_STATES)
        except InvalidStateTransition as e:
            return BringToFront_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.bring_to_front)
        except Exception as e:
            return BringToFront_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        return BringToFront_Responses(
            Result=succeeded(
                payload={"status": "ok"},
                correlation_id=req.correlation_id,
            )
        )

    def EnterMaintenance(
        self, Request: Any, *, metadata: MetadataDict
    ) -> EnterMaintenance_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.MAINTENANCE)
        except InvalidStateTransition as e:
            return EnterMaintenance_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        return EnterMaintenance_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def ExitMaintenance(
        self, Request: Any, *, metadata: MetadataDict
    ) -> ExitMaintenance_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.CONNECT)
        except InvalidStateTransition as e:
            return ExitMaintenance_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        return ExitMaintenance_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Pause(self, Request: Any, *, metadata: MetadataDict) -> Pause_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.PAUSE)
        except InvalidStateTransition as e:
            return Pause_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        return Pause_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Resume(self, Request: Any, *, metadata: MetadataDict) -> Resume_Responses:
        req = parse_request(Request)
        try:
            self._fsm.transition(GpcState.RUN)
        except InvalidStateTransition as e:
            return Resume_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        return Resume_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value},
                correlation_id=req.correlation_id,
            )
        )

    def Stop(self, Request: Any, *, metadata: MetadataDict) -> Stop_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.RUN, GpcState.PAUSE)
        except InvalidStateTransition as e:
            return Stop_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        clicked = False
        try:
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
            if is_on:
                self._exec.run(self._exec.gpc.click_start)
                clicked = True
        except Exception as e:
            log.exception("Stop failed while toggling Start button")
            return Stop_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        self._fsm.transition(GpcState.CONNECT)
        return Stop_Responses(
            Result=succeeded(
                payload={"state": self._fsm.state.value, "clicked_start": clicked},
                correlation_id=req.correlation_id,
            )
        )
