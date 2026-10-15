"""The GPC mock.

GPC is the one instrument whose state rules are enforced by code we did not
write: gpc/sila/state_machine.py is pure Python, so the mock reuses it unchanged.
These tests are therefore as much a check that the seam held as a check of the
fake -- a gpc.state.invalid rejection here comes from the real transition table.
"""

from __future__ import annotations

import time

import pytest

from .conftest import RUN_SECONDS, ask


def state(client) -> str:
    return client.InstrumentLifecycle.CurrentState.get()


def start_button(client) -> bool:
    return ask(client.InstrumentStatus.GetStartButtonState).payload["IsOn"]


def summary(client) -> dict:
    return ask(client.InstrumentStatus.GetSequenceSummary).payload


def lifecycle(client, command: str):
    return ask(getattr(client.InstrumentLifecycle, command))


@pytest.fixture(autouse=True)
def connected_and_idle(gpc):
    """Return to Connect between tests. The server is module-scoped."""
    client = gpc.client
    if state(client) == "Disconnected":
        lifecycle(client, "Connect")
    if state(client) == "Maintenance":
        lifecycle(client, "ExitMaintenance")
    if state(client) in ("Run", "Pause"):
        lifecycle(client, "Stop")
    yield


def in_maintenance(client):
    assert lifecycle(client, "EnterMaintenance").outcome == "Succeeded"


# -- identity ------------------------------------------------------------


def test_reports_the_real_server_type(gpc):
    assert gpc.client.SiLAService.ServerType.get() == "GpcSiLA"


def test_serves_the_real_feature_set(gpc):
    # 5 gpc features plus the mandatory SiLAService.
    assert len(gpc.client.SiLAService.ImplementedFeatures.get()) == 6


# -- the real state machine ----------------------------------------------


def test_connect_while_connected_is_rejected(gpc):
    """Straight from gpc/sila/state_machine.py's transition table."""
    refused = lifecycle(gpc.client, "Connect")
    assert (refused.outcome, refused.code) == ("Rejected", "gpc.state.invalid")
    assert "Disconnected" in refused.message, "the message should name the state that would be legal"


def test_disconnect_then_connect_goes_through_the_fake(gpc):
    assert lifecycle(gpc.client, "Disconnect").payload == {"state": "Disconnected"}
    assert state(gpc.client) == "Disconnected"
    assert lifecycle(gpc.client, "Connect").payload == {"state": "Connect"}


def test_ui_input_is_rejected_outside_maintenance(gpc):
    refused = ask(gpc.client.UiInput.ClickMenu, {"MenuPath": ["File", "Exit"]})
    assert (refused.outcome, refused.code) == ("Rejected", "gpc.state.invalid")


def test_ui_input_works_in_maintenance(gpc):
    in_maintenance(gpc.client)
    assert state(gpc.client) == "Maintenance"

    assert ask(gpc.client.UiInput.ClickMenu, {"MenuPath": ["File", "Open"]}).outcome == "Succeeded"
    found = ask(gpc.client.UiInput.FindControl, {"Locator": {"auto_id": "SequenceUI"}}).payload
    assert found["exists"] is True
    assert found["auto_id"] == "SequenceUI"

    lifecycle(gpc.client, "ExitMaintenance")
    assert state(gpc.client) == "Connect"


def test_missing_control_reports_not_found_rather_than_failing(gpc):
    in_maintenance(gpc.client)
    found = ask(gpc.client.UiInput.FindControl, {"Locator": {"auto_id": "NoSuchThing"}})
    assert found.outcome == "Succeeded"
    assert found.payload["exists"] is False


def test_list_children_walks_the_window(gpc):
    in_maintenance(gpc.client)
    children = ask(gpc.client.UiInput.ListChildren).payload
    assert "SequenceUI" in [child["auto_id"] for child in children]


# -- the sequence run ----------------------------------------------------


def test_click_start_moves_the_state_machine_to_run(gpc):
    assert state(gpc.client) == "Connect"
    started = ask(gpc.client.SequenceControl.ClickStart)
    assert started.payload == {"is_on": True, "state": "Run"}
    assert state(gpc.client) == "Run"
    assert start_button(gpc.client) is True


def test_the_sequence_ends_by_itself_and_the_state_returns(gpc):
    """WinGPC untoggles Start when the last injection finishes; so does the mock."""
    ask(gpc.client.SequenceControl.ClickStart)
    assert state(gpc.client) == "Run"

    deadline = time.monotonic() + RUN_SECONDS + 10
    while time.monotonic() < deadline and start_button(gpc.client):
        time.sleep(0.3)

    assert start_button(gpc.client) is False
    assert state(gpc.client) == "Connect", "the FSM should return to Connect on its own"


def test_sequence_summary_advances_during_a_run(gpc):
    """Numbers that move, so the dashboard shows something alive.

    No assumption about the idle baseline: after a completed sequence the summary
    keeps reporting that sequence's totals, which is what WinGPC does. What
    matters is that a fresh run resets the counters and then advances them.
    """
    ask(gpc.client.SequenceControl.ClickStart)

    early = summary(gpc.client)
    assert early["time_elapsed"] == "00:00", "a new run should reset the clock"

    time.sleep(RUN_SECONDS * 0.6)
    mid = summary(gpc.client)

    assert int(mid["injections_elapsed"]) > int(early["injections_elapsed"])
    assert mid["time_elapsed"] != "00:00"
    assert int(mid["injections_left"]) < 8


def test_current_step_is_empty_when_idle_and_filled_while_running(gpc):
    assert ask(gpc.client.InstrumentStatus.GetCurrentSequenceStep).payload["sequence_step"] == ""

    ask(gpc.client.SequenceControl.ClickStart)
    time.sleep(1.0)
    step = ask(gpc.client.InstrumentStatus.GetCurrentSequenceStep).payload
    assert step["sequence_step"].startswith("Step ")
    assert step["position"].startswith("Vial ")


def test_status_colours_change_with_the_run(gpc):
    """Colours come from GpcAutomation's own vocabulary, not invented strings."""
    assert ask(gpc.client.InstrumentStatus.GetSystemStatus).payload["color"] == "green"

    ask(gpc.client.SequenceControl.ClickStart)
    time.sleep(0.5)
    assert ask(gpc.client.InstrumentStatus.GetSystemStatus).payload["color"] == "yellow"


def test_pause_and_resume_follow_the_transition_table(gpc):
    ask(gpc.client.SequenceControl.ClickStart)
    assert lifecycle(gpc.client, "Pause").outcome == "Succeeded"
    assert state(gpc.client) == "Pause"

    assert lifecycle(gpc.client, "Resume").outcome == "Succeeded"
    assert state(gpc.client) == "Run"

    assert lifecycle(gpc.client, "Stop").outcome == "Succeeded"
    assert state(gpc.client) == "Connect"


def test_pause_while_not_running_is_rejected(gpc):
    assert state(gpc.client) == "Connect"
    refused = lifecycle(gpc.client, "Pause")
    assert (refused.outcome, refused.code) == ("Rejected", "gpc.state.invalid")


def test_instrument_snapshot(gpc):
    """An expected failure until upstream fixed asdict() on the system_info list."""
    snapshot = ask(gpc.client.InstrumentStatus.GetInstrumentSnapshot)
    assert snapshot.outcome == "Succeeded"
    assert snapshot.payload["system_status"] in ("green", "yellow")
    assert isinstance(snapshot.payload["system_info"], list)
    assert gpc.client.InstrumentStatus.CurrentSystemStatus.get() == snapshot.payload["system_status"]
