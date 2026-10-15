"""The simulated X3 DSC: what it remembers, and the fake TRIOS REST API over it.

Everything shared with the SDT lives in harness/trios_common.py. What is here is
what the X3 has and the SDT does not: three heat-flow sensors, an autosampler lid,
and its own /status body.

Every route the real TriosX3dscClient can call is registered. That is not
completeness for its own sake -- the backend's PropertyMonitor reads every property
of every feature on a cycle, so an unregistered route becomes a permanently
failing property on the dashboard.
"""

from __future__ import annotations

from typing import Any

from ..harness.http_fake import ApiError, RouteTable, ok
from ..harness.run_engine import RunState
from ..harness.trios_common import TriosInstrument, motion_route, register_common_routes

IDLE_TEMPERATURE = 25.0
RUN_PEAK_TEMPERATURE = 300.0

MODEL = "Discovery X3 DSC"


class X3DscInstrument(TriosInstrument):
    def __init__(self, run_seconds: float) -> None:
        super().__init__(run_seconds)
        self.lid_state = "Closed"
        self.sensors = {"A": True, "B": True, "C": True}

    def current_temperature(self) -> float:
        """Ramps while running, so the dashboard shows a number that moves."""
        snap = self.run.snapshot()
        if snap.state is not RunState.RUNNING:
            return IDLE_TEMPERATURE
        span = RUN_PEAK_TEMPERATURE - IDLE_TEMPERATURE
        return round(IDLE_TEMPERATURE + span * snap.progress, 2)

    def status_payload(self) -> dict[str, Any]:
        moving = self.motion.is_running()
        return {
            "procedureState": self.procedure_state(),
            "autosamplerMotionState": "Moving" if moving else "Idle",
            "autosamplerInMotion": moving,
            "isCalibrationState": False,
            "instrumentIsBusy": self.run.is_running() or moving,
            "currentTemperature": self.current_temperature(),
        }

    def autosampler_payload(self) -> dict[str, Any]:
        """What AutosamplerControl's observable poll reads. `isMoving` drives it."""
        return {
            "armPosition": self.arm_position,
            "lidState": self.lid_state,
            "isMoving": self.motion.is_running(),
            "isOnline": True,
        }


def build_routes(inst: X3DscInstrument, instrument_id: str) -> RouteTable:
    table = RouteTable(instrument_id)
    register_common_routes(table, inst, instrument_id, MODEL)

    @table.instrument("GET", "/status")
    def _status(req):
        return ok(inst.status_payload())

    # -- AutosamplerControl: stateful, and the second timed thing ----------

    @table.instrument("GET", "/autosampler/status")
    def _autosampler_status(req):
        return ok(inst.autosampler_payload())

    def _set_lid(state: str):
        def apply(_req):
            inst.lid_state = state

        return apply

    motion_route(table, inst, "/autosampler/load-sample", arm="Sample")
    motion_route(table, inst, "/autosampler/unload-sample", arm="Home")
    motion_route(table, inst, "/autosampler/load-reference", arm="Reference")
    motion_route(table, inst, "/autosampler/unload-reference", arm="Home")
    motion_route(table, inst, "/autosampler/park", arm="Park")
    motion_route(table, inst, "/autosampler/open-lid", on_begin=_set_lid("Open"))
    motion_route(table, inst, "/autosampler/close-lid", on_begin=_set_lid("Closed"))

    # -- SensorControl: stateful, and the X3's own ------------------------

    @table.instrument("GET", "/sensors")
    def _get_sensors(req):
        return ok({"sensors": dict(inst.sensors)})

    @table.instrument("PUT", "/sensors")
    def _set_sensors(req):
        body = req.json_body()
        incoming = body.get("sensors", body)
        if not isinstance(incoming, dict):
            raise ApiError(400, "expected a sensors object")
        merged = {**inst.sensors, **{k: bool(v) for k, v in incoming.items() if k in inst.sensors}}
        if not any(merged.values()):
            raise ApiError(400, "at least one sensor must stay enabled")
        inst.sensors = merged
        return ok({"sensors": dict(inst.sensors)})

    return table
