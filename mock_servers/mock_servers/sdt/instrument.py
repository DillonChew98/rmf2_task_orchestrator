"""The simulated SDT 650: what it remembers, and the fake TRIOS REST API over it.

Everything shared with the X3 DSC lives in harness/trios_common.py. What is here is
the SDT's own: a furnace door, gas control, pan weighing, sample mode, and its own
/status body.

The furnace is the SDT's second stateful thing, and it earns that on the same test
as the run itself -- order matters. The door must be closed before a run starts, so
a caller that gets the sequence wrong finds out here rather than in the lab.

Since the v2 envelope its ControlStart, its autosampler moves and its two status
waits are observable, like the X3's. Its furnace and gas commands stay unary.
"""

from __future__ import annotations

from typing import Any

from ..harness.http_fake import ApiError, RouteTable, ok
from ..harness.run_engine import RunState, TimedRun
from ..harness.trios_common import (
    MOTION_SECONDS,
    TriosInstrument,
    motion_route,
    register_common_routes,
)

IDLE_TEMPERATURE = 25.0
RUN_PEAK_TEMPERATURE = 1000.0

MODEL = "Discovery SDT 650"

GASES = ("Nitrogen", "Air", "Argon", "Helium")


class SdtInstrument(TriosInstrument):
    def __init__(self, run_seconds: float) -> None:
        super().__init__(run_seconds)
        # The door takes time to travel, same as the arm does.
        self.furnace_motion = TimedRun(MOTION_SECONDS)
        self.furnace_open = False
        self.sample_mode = "Sample"
        self.gas = {"selected": "Nitrogen", "flowRateMlPerMin": 50.0}
        self.tare_pans: dict[str, Any] = {}
        self.pre_weigh_pans: dict[str, Any] = {}

    def current_temperature(self) -> float:
        snap = self.run.snapshot()
        if snap.state is not RunState.RUNNING:
            return IDLE_TEMPERATURE
        span = RUN_PEAK_TEMPERATURE - IDLE_TEMPERATURE
        return round(IDLE_TEMPERATURE + span * snap.progress, 2)

    def status_payload(self) -> dict[str, Any]:
        arm_moving = self.motion.is_running()
        door_moving = self.furnace_motion.is_running()
        return {
            "procedureState": self.procedure_state(),
            "loaderInMotion": arm_moving,
            "furnaceIsMoving": door_moving,
            "isCalibrationState": False,
            "instrumentIsBusy": self.run.is_running() or arm_moving or door_moving,
            "sampleMode": self.sample_mode,
            "currentTemperature": self.current_temperature(),
        }

    def furnace_payload(self) -> dict[str, Any]:
        moving = self.furnace_motion.is_running()
        if moving:
            position = "Opening" if self.furnace_open else "Closing"
        else:
            position = "Open" if self.furnace_open else "Closed"
        return {"isOpen": self.furnace_open, "isMoving": moving, "position": position}

    def autosampler_payload(self) -> dict[str, Any]:
        return {
            "armPosition": self.arm_position,
            "isMoving": self.motion.is_running(),
            "isOnline": True,
        }

    # -- transitions ------------------------------------------------------

    def can_start(self) -> None:
        """The SDT's own precondition: you cannot run with the furnace open."""
        if self.furnace_open or self.furnace_motion.is_running():
            raise ApiError(409, "the furnace must be closed before starting a run")

    def move_furnace(self, *, opening: bool) -> None:
        if self.run.is_running():
            raise ApiError(409, "cannot move the furnace while a run is in progress", can_force=True)
        if self.furnace_motion.is_running():
            raise ApiError(409, "the furnace is already moving")
        if self.furnace_open == opening:
            return
        self.furnace_motion.start(force=True)
        self.furnace_open = opening


def build_routes(inst: SdtInstrument, instrument_id: str) -> RouteTable:
    table = RouteTable(instrument_id)
    register_common_routes(table, inst, instrument_id, MODEL)

    @table.instrument("GET", "/status")
    def _status(req):
        return ok(inst.status_payload())

    @table.instrument("GET", "/signals")
    def _signals(req):
        return ok(
            {
                "temperature": inst.current_temperature(),
                "weight": 12.345,
                "heatFlow": 0.0 if not inst.run.is_running() else 1.234,
                "gasFlow": inst.gas["flowRateMlPerMin"],
            }
        )

    # -- FurnaceControl: stateful, and the SDT's run gate ------------------

    @table.instrument("GET", "/furnace/status")
    def _furnace_status(req):
        return ok(inst.furnace_payload())

    @table.instrument("POST", "/furnace/open")
    def _furnace_open(req):
        inst.move_furnace(opening=True)
        return ok(inst.furnace_payload())

    @table.instrument("POST", "/furnace/close")
    def _furnace_close(req):
        inst.move_furnace(opening=False)
        return ok(inst.furnace_payload())

    # -- AutosamplerControl: stateful. No lid on the SDT. ------------------

    @table.instrument("GET", "/autosampler/status")
    def _autosampler_status(req):
        return ok(inst.autosampler_payload())

    motion_route(table, inst, "/autosampler/load-sample", arm="Sample")
    motion_route(table, inst, "/autosampler/unload-sample", arm="Home")
    motion_route(table, inst, "/autosampler/load-reference", arm="Reference")
    motion_route(table, inst, "/autosampler/unload-reference", arm="Home")
    motion_route(table, inst, "/autosampler/reset", arm="Home")

    @table.instrument("POST", "/autosampler/park")
    def _park(req):
        position = req.json_body().get("position", "TraySide")
        if position not in ("TraySide", "FurnaceSide"):
            raise ApiError(400, f"park position must be TraySide or FurnaceSide, got {position!r}")
        inst.begin_motion(arm_position=position)
        return ok(
            {
                "accepted": True,
                "statusPollEndpoint": "autosampler/status",
                "message": "Poll GET autosampler/status until data.isMoving is false.",
            }
        )

    # -- SampleMode: stateful ---------------------------------------------

    @table.instrument("GET", "/sample-mode")
    def _get_sample_mode(req):
        return ok({"sampleMode": inst.sample_mode})

    @table.instrument("PUT", "/sample-mode")
    def _put_sample_mode(req):
        body = req.json_body()
        mode = body.get("sampleMode", body.get("mode"))
        if mode not in ("Sample", "SampleAndReference"):
            raise ApiError(400, f"unknown sample mode {mode!r}")
        if inst.run.is_running():
            raise ApiError(409, "cannot change sample mode while a run is in progress", can_force=True)
        inst.sample_mode = mode
        return ok({"sampleMode": inst.sample_mode})

    # -- GasControl: stateful ---------------------------------------------

    @table.instrument("GET", "/gas/status")
    def _gas_status(req):
        return ok(dict(inst.gas))

    @table.instrument("POST", "/gas/select")
    def _gas_select(req):
        body = req.json_body()
        gas = body.get("gas", body.get("selected"))
        if gas not in GASES:
            raise ApiError(400, f"unknown gas {gas!r}; expected one of {list(GASES)}")
        inst.gas["selected"] = gas
        return ok(dict(inst.gas))

    @table.instrument("POST", "/gas/flow-rate")
    def _gas_flow(req):
        body = req.json_body()
        rate = body.get("flowRateMlPerMin", body.get("flowRate"))
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            raise ApiError(400, f"flow rate must be a number, got {rate!r}") from None
        if not 0 <= rate <= 200:
            raise ApiError(400, "flow rate must be between 0 and 200 ml/min")
        inst.gas["flowRateMlPerMin"] = rate
        return ok(dict(inst.gas))

    # -- PanWeighing: canned. Values in and out, nothing sequences them. ---

    @table.instrument("GET", "/tare")
    def _get_tare(req):
        return ok({"pans": dict(inst.tare_pans)})

    @table.instrument("POST", "/tare")
    def _start_tare(req):
        inst.tare_pans = {"sample": 21.05, "reference": 20.98}
        return ok({"accepted": True, "pans": dict(inst.tare_pans)})

    @table.instrument("GET", "/pre-weigh")
    def _get_pre_weigh(req):
        return ok({"pans": dict(inst.pre_weigh_pans)})

    @table.instrument("POST", "/pre-weigh")
    def _start_pre_weigh(req):
        inst.pre_weigh_pans = {"sample": 34.12, "reference": 20.98}
        return ok({"accepted": True, "pans": dict(inst.pre_weigh_pans)})

    return table
