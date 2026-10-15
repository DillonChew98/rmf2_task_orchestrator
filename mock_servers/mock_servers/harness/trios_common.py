"""What the X3 DSC and SDT mocks have in common.

Both are TRIOS instruments behind the same REST shape, so roughly thirty of their
fifty-odd routes are identical: the queue, the results export, the application
settings, authentication, the dialogs, and the blocking status waits. Those live
here. Each instrument's own module adds what is genuinely its own -- sensors and
an autosampler lid for the X3, a furnace and gas control for the SDT -- and
supplies its own /status body, because the two report different objects.

This was factored out while writing the second instrument, not before. Guessing at
the shared surface from one example is how you get an abstraction that fits
neither.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from .http_fake import ApiError, RouteTable, ok
from .run_engine import IllegalTransition, RunState, TimedRun

# Simulated motion must finish inside the real autosampler poll window:
# _AUTOSAMPLER_START_TIMEOUT = 5.0s, polled every 0.5s.
MOTION_SECONDS = 2.0


class TriosInstrument:
    """State and transitions shared by both TRIOS mocks."""

    def __init__(self, run_seconds: float) -> None:
        self.run = TimedRun(run_seconds)
        self.motion = TimedRun(MOTION_SECONDS)
        self.lock = threading.RLock()

        self.queue: list[dict[str, Any]] = []
        self.completed: list[str] = []
        self.arm_position = "Home"
        self.autosampler_settings: dict[str, Any] = {"trayMap": "default", "autoLoad": True}
        self.reference_pans: dict[str, Any] = {"1": {"panNumber": 1, "mass": 52.1}}
        self.app_settings = {
            "analysis": {"autoAnalyse": False},
            "json": {"pretty": True},
            "lims": {"enabled": False},
            "user": {"operator": "mock"},
        }
        self.ui_enabled = True
        self.export_jobs: dict[str, dict[str, Any]] = {}

    # -- derived views ----------------------------------------------------

    def procedure_state(self) -> str:
        snap = self.run.snapshot()
        if snap.state is not RunState.RUNNING:
            return "Idle"
        if snap.progress < 0.1:
            return "PreTest"
        if snap.progress > 0.9:
            return "PostTest"
        return "Test"

    def status_payload(self) -> dict[str, Any]:  # pragma: no cover - subclasses override
        raise NotImplementedError

    def queue_status_payload(self) -> dict[str, Any]:
        """What the callers' polling loops read. `isRunning` is the field that matters."""
        snap = self.run.snapshot()
        running = snap.state is RunState.RUNNING
        with self.lock:
            return {
                "isRunning": running,
                "currentExperimentId": snap.experiment_id if running else None,
                "currentExperimentIndex": 0 if running and self.queue else -1,
                "count": len(self.queue),
            }

    # -- transitions ------------------------------------------------------

    def can_start(self) -> None:
        """Hook for instrument-specific preconditions, e.g. the SDT's furnace door."""

    def start_run(self, *, force: bool) -> None:
        self.can_start()
        with self.lock:
            experiment_id = self.queue[0]["experimentId"] if self.queue else None
        try:
            self.run.start(experiment_id=experiment_id, force=force)
        except IllegalTransition as e:
            # canForce is how the real API says "this would interrupt a run; pass
            # force=true if you mean it". The client surfaces it on its error type.
            raise ApiError(409, str(e), can_force=True) from None

    def stop_run(self) -> None:
        try:
            self.run.abort()
        except IllegalTransition as e:
            raise ApiError(409, str(e)) from None
        with self.lock:
            if self.queue:
                self.queue[0]["state"] = "Cancelled"

    def begin_motion(self, *, arm_position: str | None = None) -> None:
        if self.run.is_running():
            raise ApiError(
                409, "cannot move the autosampler while a run is in progress", can_force=True
            )
        self.motion.start(force=True)
        if arm_position is not None:
            self.arm_position = arm_position

    def add_experiments(self, body: dict[str, Any]) -> list[str]:
        raw = body.get("experiments")
        entries = raw if isinstance(raw, list) and raw else [body or {"name": "unnamed"}]
        added: list[str] = []
        with self.lock:
            for entry in entries:
                experiment_id = str(uuid.uuid4())
                self.queue.append(
                    {
                        "experimentId": experiment_id,
                        "name": entry.get("name", f"experiment-{len(self.queue) + 1}"),
                        "state": "Queued",
                        "payload": entry,
                    }
                )
                added.append(experiment_id)
        return added

    def find_experiment(self, experiment_id: str) -> dict[str, Any]:
        with self.lock:
            for entry in self.queue:
                if entry["experimentId"] == experiment_id:
                    return entry
        raise ApiError(404, f"no experiment {experiment_id!r} in the queue")

    def settle_completed_run(self) -> None:
        """Mark the head experiment done once its run has finished.

        Driven from queue reads rather than a timer, matching how TimedRun settles:
        whichever caller looks first does the work.
        """
        snap = self.run.snapshot()
        if snap.state is not RunState.COMPLETE:
            return
        with self.lock:
            for entry in self.queue:
                if entry["experimentId"] == snap.experiment_id and entry["state"] != "Completed":
                    entry["state"] = "Completed"
                    self.completed.append(entry["experimentId"])


def register_common_routes(table: RouteTable, inst: TriosInstrument, instrument_id: str, model: str) -> None:
    """Every route both TRIOS instruments answer identically."""

    # -- status waits: stateful, and genuinely blocking --------------------

    @table.instrument("POST", "/status/wait-idle")
    def _wait_idle(req):
        # The real TRIOS API blocks server-side here rather than having the client
        # poll. That is what keeps a gRPC stream open for the length of a run.
        with table.released():
            idle = inst.run.wait_until_idle(req.q_int("timeout", 300))
        if idle:
            return ok(inst.status_payload())
        raise ApiError(408, "timed out waiting for the instrument to go idle")

    @table.instrument("POST", "/status/wait-test-started")
    def _wait_started(req):
        with table.released():
            started = inst.run.wait_for_start(req.q_int("timeout", 300))
        if started:
            return ok(inst.status_payload())
        raise ApiError(408, "timed out waiting for the test to start")

    @table.instrument("GET", "/errors")
    def _errors(req):
        return ok({"hasErrors": False, "errors": []})

    # -- queue: stateful ---------------------------------------------------

    @table.instrument("GET", "/queue/experiments")
    def _list_queue(req):
        inst.settle_completed_run()
        index = req.q_int("index")
        with inst.lock:
            items = list(inst.queue)
        if index is not None:
            items = items[index : index + 1]
        return ok({"experiments": items, "count": len(items)})

    @table.instrument("GET", "/queue/status")
    def _queue_status(req):
        inst.settle_completed_run()
        return ok(inst.queue_status_payload())

    @table.instrument("GET", "/queue/validate")
    def _validate_queue(req):
        return ok({"isValid": True, "issues": []})

    @table.instrument("POST", "/queue/experiments/new")
    def _add_new(req):
        return ok({"addedExperimentIds": inst.add_experiments(req.json_body())})

    @table.instrument("POST", "/queue/experiments/from-sequence")
    def _add_from_sequence(req):
        return ok({"addedExperimentIds": inst.add_experiments(req.json_body())})

    @table.instrument("POST", "/queue/experiments/copy")
    def _copy(req):
        source = req.json_body().get("experimentId")
        if source:
            inst.find_experiment(str(source))
        return ok({"addedExperimentIds": inst.add_experiments(req.json_body())})

    @table.instrument("POST", "/queue/experiments/validate")
    def _validate_body(req):
        return ok({"isValid": True, "issues": []})

    @table.instrument("GET", "/queue/experiments/{experimentId}")
    def _get_experiment(req):
        return ok(inst.find_experiment(req.params["experimentId"]))

    @table.instrument("PATCH", "/queue/experiments/{experimentId}")
    def _modify_experiment(req):
        entry = inst.find_experiment(req.params["experimentId"])
        with inst.lock:
            entry["payload"].update(req.json_body())
        return ok(entry)

    @table.instrument("DELETE", "/queue/experiments/{experimentId}")
    def _remove_experiment(req):
        entry = inst.find_experiment(req.params["experimentId"])
        with inst.lock:
            if inst.run.is_running() and entry["experimentId"] == inst.run.snapshot().experiment_id:
                raise ApiError(409, "cannot remove the experiment that is running", can_force=True)
            inst.queue.remove(entry)
        return ok(True)

    @table.instrument("PUT", "/queue/experiments/{experimentId}/position")
    def _move_experiment(req):
        entry = inst.find_experiment(req.params["experimentId"])
        target = req.json_body().get("position", 0)
        with inst.lock:
            inst.queue.remove(entry)
            inst.queue.insert(max(0, min(int(target), len(inst.queue))), entry)
        return ok(True)

    @table.instrument("GET", "/queue/experiments/{experimentId}/validate")
    def _validate_one(req):
        inst.find_experiment(req.params["experimentId"])
        return ok({"isValid": True, "issues": []})

    # -- autosampler settings and reference pans ---------------------------

    @table.instrument("GET", "/autosampler/settings")
    def _get_autosampler_settings(req):
        return ok(dict(inst.autosampler_settings))

    @table.instrument("PUT", "/autosampler/settings")
    def _put_autosampler_settings(req):
        inst.autosampler_settings.update(req.json_body())
        return ok(dict(inst.autosampler_settings))

    @table.instrument("GET", "/reference-pans")
    def _get_pans(req):
        return ok({"pans": dict(inst.reference_pans)})

    @table.instrument("PUT", "/reference-pans")
    def _put_pans(req):
        body = req.json_body()
        incoming = body.get("pans", body)
        if isinstance(incoming, dict):
            inst.reference_pans.update(incoming)
        return ok({"pans": dict(inst.reference_pans)})

    @table.instrument("DELETE", "/reference-pans/{panNumber}")
    def _delete_pan(req):
        number = req.params["panNumber"]
        if number not in inst.reference_pans:
            raise ApiError(404, f"no reference pan {number}")
        del inst.reference_pans[number]
        return ok(True)

    # -- results export: stateful-lite -------------------------------------

    @table.instrument("POST", "/results/export-json")
    def _export(req):
        job_id = str(uuid.uuid4())
        inst.export_jobs[job_id] = {"jobId": job_id, "state": "running"}
        return ok({"jobId": job_id})

    @table.instrument("GET", "/results/export-json/jobs/{jobId}")
    def _export_job(req):
        job = inst.export_jobs.get(req.params["jobId"])
        if job is None:
            raise ApiError(404, f"no export job {req.params['jobId']}")
        # First read reports running, the next reports completed -- enough for a
        # caller to exercise its polling loop without a second timer.
        if job["state"] == "running":
            job["state"] = "completed"
            return ok({"jobId": job["jobId"], "state": "running"})
        return ok(
            {
                "jobId": job["jobId"],
                "state": "completed",
                "results": {"samples": list(inst.completed), "note": "simulated export"},
            }
        )

    # -- run control -------------------------------------------------------

    @table.common("POST", "/control/start")
    def _control_start(req):
        inst.start_run(force=req.q_bool("force"))
        return ok(True)

    @table.common("POST", "/control/stop")
    def _control_stop(req):
        inst.stop_run()
        return ok(True)

    # -- canned from here down ---------------------------------------------

    @table.common("POST", "/auth/login")
    def _login(req):
        return ok({"accessToken": "mock-access-token", "refreshToken": "mock-refresh-token"})

    @table.common("POST", "/auth/refresh-token")
    def _refresh(req):
        return ok({"accessToken": "mock-access-token", "refreshToken": "mock-refresh-token"})

    @table.common("POST", "/auth/verify-token")
    def _verify(req):
        return ok({"valid": True})

    @table.common("GET", "/health")
    def _health(req):
        return ok({"status": "healthy", "mock": True})

    @table.common("GET", "/instances")
    def _instances(req):
        return ok({"instances": [{"instrumentId": instrument_id, "name": f"{model} (mock)"}]})

    @table.common("GET", "/info")
    def _info(req):
        return ok(
            {
                "instrumentId": instrument_id,
                "model": model,
                "serialNumber": "MOCK-0001",
                "firmwareVersion": "0.0.0-mock",
            }
        )

    @table.common("POST", "/app/show")
    def _show(req):
        return ok(True)

    @table.common("POST", "/app/hide")
    def _hide(req):
        return ok(True)

    @table.common("GET", "/app/ui-enabled")
    def _get_ui(req):
        return ok({"enabled": inst.ui_enabled})

    @table.common("PUT", "/app/ui-enabled")
    def _set_ui(req):
        inst.ui_enabled = bool(req.json_body().get("enabled", True))
        return ok({"enabled": inst.ui_enabled})

    for _name in ("analysis", "json", "lims", "user"):

        @table.common("GET", f"/app/settings/{_name}")
        def _get_setting(req, _key=_name):
            return ok(dict(inst.app_settings[_key]))

        @table.common("PUT", f"/app/settings/{_name}")
        def _set_setting(req, _key=_name):
            inst.app_settings[_key].update(req.json_body())
            return ok(dict(inst.app_settings[_key]))

    @table.common("POST", "/dialog/show-message")
    def _message(req):
        return ok(True)

    @table.common("POST", "/dialog/notification")
    def _notification(req):
        return ok(True)


def motion_route(table: RouteTable, inst: TriosInstrument, pattern: str, *, arm: str | None = None,
                 on_begin=None):
    """Register a POST that starts simulated arm motion and returns 202-style."""

    @table.instrument("POST", pattern)
    def _motion(req, _arm=arm, _on_begin=on_begin):
        inst.begin_motion(arm_position=_arm)
        if _on_begin is not None:
            _on_begin(req)
        return ok(
            {
                "accepted": True,
                "statusPollEndpoint": "autosampler/status",
                "message": "Poll GET autosampler/status until data.isMoving is false.",
            }
        )

    return _motion
