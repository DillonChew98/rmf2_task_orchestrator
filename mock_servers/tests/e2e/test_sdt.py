"""The SDT mock.

The shared TRIOS behaviour is covered in depth by test_x3dsc.py. This file
concentrates on what is the SDT's own: the furnace door, gas control and sample
mode. Since the v2 envelope its ControlStart is observable, like the X3's.
"""

from __future__ import annotations

import json
import time

import pytest

from .conftest import RUN_SECONDS, ask, result_of, send, wait_done

FURNACE_MOTION_S = 2.0


def status(client) -> dict:
    return json.loads(client.InstrumentStatus.CurrentInstrumentStatus.get())["data"]


def furnace(client) -> dict:
    return json.loads(client.FurnaceControl.CurrentFurnaceStatus.get())["data"]


def gas(client) -> dict:
    return json.loads(client.GasControl.CurrentGasStatus.get())["data"]


def arm(client) -> dict:
    return json.loads(client.AutosamplerControl.CurrentAutosamplerStatus.get())["data"]


def queue_status(client) -> dict:
    return ask(client.QueueManagement.GetQueueStatusJson, {"QueueType": ""}).payload["Body"]["data"]


def start_run(client):
    return send(
        client.RunControl.ControlStart, {"WaitForReady": "false", "ReadyTimeoutSeconds": 0, "Force": False}
    )


def wait_until_idle(client):
    return result_of(send(client.InstrumentStatus.WaitUntilIdle, timeout_seconds=30))


@pytest.fixture(autouse=True)
def idle_and_closed(sdt):
    """Each test starts idle with the furnace closed."""
    wait_until_idle(sdt.client)
    if furnace(sdt.client)["isOpen"]:
        ask(sdt.client.FurnaceControl.CloseFurnace)
        time.sleep(FURNACE_MOTION_S + 0.3)
    yield


# -- identity ------------------------------------------------------------


def test_reports_the_real_server_type(sdt):
    assert sdt.client.SiLAService.ServerType.get() == "TriosSDTSiLA"


def test_serves_the_real_feature_set(sdt):
    # 15 sdt features plus the mandatory SiLAService.
    assert len(sdt.client.SiLAService.ImplementedFeatures.get()) == 16


# -- the run lifecycle ---------------------------------------------------


def test_run_reports_running_then_returns_to_idle(sdt):
    cmd = start_run(sdt.client)
    time.sleep(1.0)

    mid = status(sdt.client)
    assert mid["procedureState"] in {"PreTest", "Test", "PostTest"}
    assert mid["instrumentIsBusy"] is True

    assert result_of(cmd).outcome == "Succeeded"
    assert status(sdt.client)["procedureState"] == "Idle"


def test_wait_until_idle_blocks_for_the_run(sdt):
    cmd = start_run(sdt.client)
    time.sleep(0.3)

    t0 = time.monotonic()
    assert wait_until_idle(sdt.client).outcome == "Succeeded"
    blocked = time.monotonic() - t0

    assert blocked > RUN_SECONDS / 2, "WaitUntilIdle returned without waiting"
    wait_done(cmd)


def test_second_start_during_a_run_is_rejected_with_the_trios_409(sdt):
    first = start_run(sdt.client)
    time.sleep(0.5)

    second = result_of(start_run(sdt.client))
    assert (second.outcome, second.code) == ("Rejected", "trios.http.409")
    assert "already in progress" in second.message

    wait_done(first)


def test_temperature_ramps_much_higher_than_the_dsc(sdt):
    """The SDT is a 1000 degC instrument, the X3 tops out at 300."""
    cmd = start_run(sdt.client)
    time.sleep(RUN_SECONDS * 0.75)
    hot = status(sdt.client)["currentTemperature"]
    wait_done(cmd)
    assert hot > 300.0


# -- the furnace: the SDT's own stateful thing ---------------------------


def test_furnace_starts_closed(sdt):
    assert furnace(sdt.client)["isOpen"] is False
    assert furnace(sdt.client)["position"] == "Closed"


def test_opening_the_furnace_takes_time(sdt):
    assert ask(sdt.client.FurnaceControl.OpenFurnace).outcome == "Succeeded"

    mid = furnace(sdt.client)
    assert mid["isMoving"] is True
    assert mid["position"] == "Opening"

    time.sleep(FURNACE_MOTION_S + 0.3)
    settled = furnace(sdt.client)
    assert settled["isMoving"] is False
    assert settled["position"] == "Open"


def test_opening_an_open_furnace_is_rejected(sdt):
    ask(sdt.client.FurnaceControl.OpenFurnace)
    time.sleep(FURNACE_MOTION_S + 0.3)

    again = ask(sdt.client.FurnaceControl.OpenFurnace)
    assert again.outcome == "Rejected"
    assert again.code.startswith("trios.http.4")


def test_a_run_will_not_start_with_the_furnace_open(sdt):
    """The reason FurnaceControl is on the stateful side: order matters here."""
    ask(sdt.client.FurnaceControl.OpenFurnace)
    time.sleep(FURNACE_MOTION_S + 0.3)

    refused = result_of(start_run(sdt.client))
    assert refused.outcome == "Rejected"
    assert "furnace must be closed" in refused.message
    assert queue_status(sdt.client)["isRunning"] is False


def test_the_furnace_will_not_move_during_a_run(sdt):
    cmd = start_run(sdt.client)
    time.sleep(0.5)

    refused = ask(sdt.client.FurnaceControl.OpenFurnace)
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")

    wait_done(cmd)


# -- gas and sample mode -------------------------------------------------


def test_gas_selection_is_remembered(sdt):
    assert ask(sdt.client.GasControl.SelectGas, {"gas": "Argon"}).outcome == "Succeeded"
    assert gas(sdt.client)["selected"] == "Argon"


def test_unknown_gas_is_rejected(sdt):
    refused = ask(sdt.client.GasControl.SelectGas, {"gas": "Xenon"})
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")


def test_flow_rate_is_range_checked(sdt):
    assert ask(sdt.client.GasControl.SetGasFlowRate, {"flowRateMlPerMin": 75}).outcome == "Succeeded"
    assert gas(sdt.client)["flowRateMlPerMin"] == 75.0

    refused = ask(sdt.client.GasControl.SetGasFlowRate, {"flowRateMlPerMin": 500})
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")


def test_sample_mode_is_remembered(sdt):
    assert (
        ask(sdt.client.SampleMode.PutSampleMode, {"sampleMode": "SampleAndReference"}).outcome == "Succeeded"
    )
    mode = ask(sdt.client.SampleMode.GetSampleModeJson).payload["Body"]["data"]["sampleMode"]
    assert mode == "SampleAndReference"
    assert status(sdt.client)["sampleMode"] == "SampleAndReference"
    ask(sdt.client.SampleMode.PutSampleMode, {"sampleMode": "Sample"})


def test_sample_mode_cannot_change_mid_run(sdt):
    cmd = start_run(sdt.client)
    time.sleep(0.5)

    refused = ask(sdt.client.SampleMode.PutSampleMode, {"sampleMode": "SampleAndReference"})
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")

    wait_done(cmd)


def test_park_position_is_validated(sdt):
    parked = result_of(send(sdt.client.AutosamplerControl.ParkAutosampler, {"Position": "TraySide"}))
    assert parked.outcome == "Succeeded"
    assert arm(sdt.client)["armPosition"] == "TraySide"

    refused = result_of(send(sdt.client.AutosamplerControl.ParkAutosampler, {"Position": "Nowhere"}))
    assert refused.outcome == "Rejected"
    assert refused.code.startswith("trios.http.4")
