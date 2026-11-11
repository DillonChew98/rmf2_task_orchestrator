"""Utility SiLA feature implementation."""

from __future__ import annotations

from typing import Any

from sila2.server import MetadataDict, SilaServer

from ..command import CONNECTED_STATES, handle_command
from ..executor import GpcExecutor
from ..gpc_sila.utility.utility_base import UtilityBase
from ..gpc_sila.utility.utility_types import Screenshot_Responses
from ..state_machine import GpcStateMachine


class UtilityImpl(UtilityBase):
    def __init__(
        self,
        parent_server: SilaServer,
        executor: GpcExecutor,
        fsm: GpcStateMachine,
    ) -> None:
        super().__init__(parent_server)
        self._exec = executor
        self._fsm = fsm

    def Screenshot(self, Request: Any, *, metadata: MetadataDict) -> Screenshot_Responses:
        def work(payload: dict[str, Any], _cid: str) -> dict[str, Any]:
            filename = payload.get("Filename")
            if filename is None:
                filename = ""
            if not isinstance(filename, str):
                raise ValueError("Payload.Filename must be a string.")
            target = filename or "gpc_screenshot.png"
            self._exec.run(self._exec.gpc.screenshot, target)
            return {"filename": target}

        return Screenshot_Responses(
            Result=handle_command(
                request=Request,
                fsm=self._fsm,
                allowed=CONNECTED_STATES,
                work=work,
            )
        )
