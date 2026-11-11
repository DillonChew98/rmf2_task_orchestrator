"""SequenceControl SiLA feature implementation."""

from __future__ import annotations

import logging
from typing import Any

from sila2.server import MetadataDict, SilaServer
from sila_common.envelope import parse_request, succeeded

from ..command import failed_automation, rejected_state
from ..executor import GpcExecutor
from ..gpc_sila.sequencecontrol.sequencecontrol_base import SequenceControlBase
from ..gpc_sila.sequencecontrol.sequencecontrol_types import ClickStart_Responses
from ..state_machine import GpcState, GpcStateMachine, InvalidStateTransition

log = logging.getLogger(__name__)


class SequenceControlImpl(SequenceControlBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm

    def ClickStart(self, Request: Any, *, metadata: MetadataDict) -> ClickStart_Responses:
        req = parse_request(Request)
        try:
            self._fsm.require(GpcState.CONNECT, GpcState.RUN)
        except InvalidStateTransition as e:
            return ClickStart_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        try:
            self._exec.run(self._exec.gpc.click_start)
            is_on = self._exec.run(self._exec.gpc.get_start_button_state)
        except Exception as e:
            log.exception("ClickStart failed")
            return ClickStart_Responses(
                Result=failed_automation(e, correlation_id=req.correlation_id)
            )
        target = GpcState.RUN if is_on else GpcState.CONNECT
        try:
            self._fsm.transition(target)
        except InvalidStateTransition as e:
            return ClickStart_Responses(
                Result=rejected_state(e, correlation_id=req.correlation_id)
            )
        return ClickStart_Responses(
            Result=succeeded(
                payload={"is_on": bool(is_on), "state": target.value},
                correlation_id=req.correlation_id,
            )
        )
