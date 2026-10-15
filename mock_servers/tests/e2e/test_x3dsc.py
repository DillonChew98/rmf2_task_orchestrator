"""The X3 DSC mock, driven over real gRPC by a real SilaClient."""

from __future__ import annotations

import json
import time

import pytest

from .conftest import RUN_SECONDS, ask, result_of, send, updates_of, wait_done

# The real RunControl polls every RUN_POLL_INTERVAL = 2.0s
# (trios_common/trios_common/sila.py).
RUN_POLL_INTERVAL = 2.0


def status(client) -> dict:
    return json.loads(client.InstrumentStatus.CurrentInstrumentStatus.get())["data"]


def arm(client) -> dict:
    return json.loads(client.AutosamplerControl.CurrentAutosamplerStatus.get())["data"]


def queue_status(client) -> dict:
    return ask(client.QueueManagement.GetQueueStatusJson, {"QueueType": ""}).payload["Body"]["data"]


def start_run(client):
    return send(
        client.RunControl.ControlStart, {"WaitForReady": "false", "ReadyTimeoutSeconds": 0, "Force": False}
    )


def wait_until_idle(client):
    return send(client.InstrumentStatus.WaitUntilIdle, timeout_seconds=30)


@pytest.fixture(autouse=True)
def idle_between_tests(x3dsc):
    """Each test starts from idle. Module-scoped server, so state carries over."""
    result_of(wait_until_idle(x3dsc.client))
    yield


# -- identity ------------------------------------------------------------


def test_reports_the_real_server_type(x3dsc):
    """Not 'Mock...'. The backend dispatches on server_type, so it must match."""
    assert x3dsc.client.SiLAService.ServerType.get() == "TriosX3DSCSiLA"


def test_uuid_is_stable_and_encodes_the_port(x3dsc):
    uuid = x3dsc.client.SiLAService.ServerUUID.get()
    assert uuid == f"00000000-0000-4000-8000-{x3dsc.port:012d}"


def test_name_marks_it_as_a_mock_for_humans(x3dsc):
    assert "MOCK" in x3dsc.client.SiLAService.ServerName.get()


def test_serves_the_real_feature_set(x3dsc):
    # 12 x3dsc features plus the mandatory SiLAService.
    assert len(x3dsc.client.SiLAService.ImplementedFeatures.get()) == 13


# -- the run lifecycle ---------------------------------------------------


def test_starts_idle(x3dsc):
    assert status(x3dsc.client)["procedureState"] == "Idle"
    assert queue_status(x3dsc.client)["isRunning"] is False


def test_queueing_an_experiment_returns_an_id(x3dsc):
    added = ask(x3dsc.client.QueueManagement.AddNewExperiments, {"QueueType": "", "Body": {"name": "run-1"}})
    assert added.outcome == "Succeeded"
    assert len(added.payload["Body"]["data"]["addedExperimentIds"]) == 1
    assert queue_status(x3dsc.client)["count"] >= 1


def test_run_reports_running_then_returns_to_idle(x3dsc):
    cmd = start_run(x3dsc.client)
    time.sleep(1.0)

    mid = status(x3dsc.client)
    assert mid["procedureState"] in {"PreTest", "Test", "PostTest"}
    assert mid["instrumentIsBusy"] is True
    assert queue_status(x3dsc.client)["isRunning"] is True

    assert result_of(cmd).outcome == "Succeeded"
    assert status(x3dsc.client)["procedureState"] == "Idle"
    assert queue_status(x3dsc.client)["isRunning"] is False


def test_run_takes_the_configured_time(x3dsc):
    t0 = time.monotonic()
    updates_of(start_run(x3dsc.client))
    elapsed = time.monotonic() - t0

    # The lower bound is the half that matters -- it catches a mock that returned
    # instantly. The upper bound stays generous because the poll granularity is 2s.
    assert elapsed >= RUN_SECONDS
    assert elapsed < RUN_SECONDS + 3 * RUN_POLL_INTERVAL


def test_run_streams_running_updates_for_its_whole_duration(x3dsc):
    """The backend's BACKLOG asks for a command slow enough to outlive a keepalive."""
    updates = updates_of(start_run(x3dsc.client))

    assert len(updates) >= 3
    assert {update.outcome for update in updates} == {"Running"}
    running_flags = [update.payload["Body"]["data"]["isRunning"] for update in updates]
    assert any(running_flags), "no update ever reported the run active"
    assert running_flags[-1] is False, "the last update should report the run done"


def test_temperature_ramps_during_a_run(x3dsc):
    """A number that moves, so the dashboard shows something alive."""
    assert status(x3dsc.client)["currentTemperature"] == pytest.approx(25.0)

    cmd = start_run(x3dsc.client)
    time.sleep(RUN_SECONDS / 2)
    hot = status(x3dsc.client)["currentTemperature"]
    wait_done(cmd)

    assert hot > 25.0
    assert status(x3dsc.client)["currentTemperature"] == pytest.approx(25.0)


# -- illegal transitions -------------------------------------------------


def test_second_start_during_a_run_is_rejected_with_the_trios_409(x3dsc):
    """The whole reason for faking urlopen rather than the whole client.

    The 409 travels the real client's error path -- _decode_response, the error
    details, TriosClientError, trios_error_envelope -- and arrives as a Rejected
    envelope carrying the HTTP status. A Failed one here would mean the mock
    answered with the wrong status, and an InternalError that a raw Python
    exception escaped.
    """
    first = start_run(x3dsc.client)
    time.sleep(0.5)

    second = result_of(start_run(x3dsc.client))
    assert (second.outcome, second.code) == ("Rejected", "trios.http.409")
    assert "already in progress" in second.message
    assert second.details["canForce"] is True

    wait_done(first)


def test_stop_with_nothing_running_is_rejected(x3dsc):
    stopped = ask(x3dsc.client.RunControl.ControlStop, {"Force": False})
    assert stopped.outcome == "Rejected"
    assert stopped.code.startswith("trios.http.4")


def test_moving_the_autosampler_during_a_run_is_refused(x3dsc):
    cmd = start_run(x3dsc.client)
    time.sleep(0.5)

    park = result_of(send(x3dsc.client.AutosamplerControl.ParkAutosampler, {"Position": "Home"}))
    assert (park.outcome, park.code) == ("Rejected", "trios.http.409")
    assert park.details["canForce"] is True

    wait_done(cmd)


# -- abort ---------------------------------------------------------------


def test_abort_stops_the_run_promptly(x3dsc):
    cmd = start_run(x3dsc.client)
    time.sleep(1.0)
    assert queue_status(x3dsc.client)["isRunning"] is True

    t0 = time.monotonic()
    assert ask(x3dsc.client.RunControl.ControlStop, {"Force": False}).outcome == "Succeeded"
    assert queue_status(x3dsc.client)["isRunning"] is False
    assert time.monotonic() - t0 < 1.0

    wait_done(cmd)


# -- blocking waits ------------------------------------------------------


def test_wait_until_idle_blocks_for_the_run(x3dsc):
    cmd = start_run(x3dsc.client)
    time.sleep(0.3)

    t0 = time.monotonic()
    waited = result_of(wait_until_idle(x3dsc.client))
    blocked = time.monotonic() - t0

    assert waited.outcome == "Succeeded"
    assert blocked > RUN_SECONDS / 2, "WaitUntilIdle returned without waiting"
    wait_done(cmd)


def test_wait_until_idle_streams_status_while_it_waits(x3dsc):
    """The real impl polls GET /status every 0.5 s while the wait call blocks."""
    cmd = start_run(x3dsc.client)
    time.sleep(0.3)

    updates = updates_of(wait_until_idle(x3dsc.client))

    assert len(updates) >= RUN_SECONDS, "fewer than one status update a second"
    assert updates[0].payload["Body"]["data"]["instrumentIsBusy"] is True
    wait_done(cmd)


def test_a_stop_answers_at_once_while_someone_waits(x3dsc):
    cmd = start_run(x3dsc.client)
    time.sleep(0.3)
    waiter = wait_until_idle(x3dsc.client)
    time.sleep(0.5)

    t0 = time.monotonic()
    assert ask(x3dsc.client.RunControl.ControlStop, {"Force": False}).outcome == "Succeeded"
    assert time.monotonic() - t0 < 1.0
    assert result_of(waiter).outcome == "Succeeded"
    wait_done(cmd)


def test_wait_until_idle_returns_at_once_when_already_idle(x3dsc):
    t0 = time.monotonic()
    assert result_of(wait_until_idle(x3dsc.client)).outcome == "Succeeded"
    assert time.monotonic() - t0 < 1.5


# -- the autosampler, the other timed thing ------------------------------


def test_autosampler_motion_is_timed_and_streams(x3dsc):
    cmd = send(x3dsc.client.AutosamplerControl.ParkAutosampler, {"Position": "Home"})
    updates = updates_of(cmd)

    assert len(updates) >= 2
    moving = [update.payload["Body"]["data"]["isMoving"] for update in updates]
    assert any(moving), "the arm never reported moving"
    assert moving[-1] is False
    assert result_of(cmd).outcome == "Succeeded"


def test_autosampler_records_where_the_arm_went(x3dsc):
    loaded = result_of(send(x3dsc.client.AutosamplerControl.LoadSamplePan, {"position": 1}))
    assert loaded.outcome == "Succeeded"
    assert arm(x3dsc.client)["armPosition"] == "Sample"


# -- stateful odds and ends ----------------------------------------------


def test_sensors_remember_what_was_set(x3dsc):
    assert ask(x3dsc.client.SensorControl.SetSensors, {"sensors": {"C": False}}).outcome == "Succeeded"
    sensors = json.loads(x3dsc.client.SensorControl.CurrentSensorsJson.get())["data"]["sensors"]
    assert sensors["C"] is False
    assert sensors["A"] is True


def test_disabling_every_sensor_is_rejected(x3dsc):
    refused = ask(x3dsc.client.SensorControl.SetSensors, {"sensors": {"A": False, "B": False, "C": False}})
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")


def test_unknown_experiment_is_a_not_found(x3dsc):
    missing = ask(x3dsc.client.QueueManagement.GetExperimentDetails, {"ExperimentId": "does-not-exist"})
    assert (missing.outcome, missing.code) == ("Rejected", "trios.http.404")
