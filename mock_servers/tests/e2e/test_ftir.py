"""The FTIR mock.

The interesting part here is the measurement round-trip. The fake writes a genuine
ASM document through ftir_automation's own writer, and MeasurementService reads it
back to fill seven of its properties. So those properties going from empty to real
values proves the whole chain works with nothing faked in the SiLA layer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mock_servers.harness.instrument_root import find_instrument_root

from .conftest import RUN_SECONDS, ask, result_of, send, updates_of

ASM_PROPERTIES = (
    "DetectorType",
    "BeamsplitterType",
    "EquipmentSerialNumber",
    "LastSampleIdentifier",
    "LastMeasurementIdentifier",
    "LastAnalyst",
    "LastMeasurementTime",
)

MEASURE = {
    "Resolution": 4.0,
    "SampleScans": 16,
    "BackgroundScans": 16,
    "OpticalVelocity": 10.0,
    "DetectorGain": 1.0,
    "IncludeData": False,
    "ExportAsm": True,
}


def measure(client, sample_name: str = "mock-sample", **extra):
    return send(client.MeasurementService.MeasureSample, {**MEASURE, "SampleName": sample_name, **extra})


def stage_position(client) -> list[str]:
    return ask(client.StageControlService.GetStagePosition, {"Device": ""}).payload["Response"]


@pytest.fixture(autouse=True)
def connected(ftir):
    assert ask(ftir.client.InstrumentConnection.Connect).outcome == "Succeeded"
    yield


# -- identity ------------------------------------------------------------


def test_reports_the_real_server_type(ftir):
    assert ftir.client.SiLAService.ServerType.get() == "FtirSiLA"


def test_serves_the_real_feature_set(ftir):
    # 6 ftir features plus the mandatory SiLAService.
    assert len(ftir.client.SiLAService.ImplementedFeatures.get()) == 7


# -- connection ----------------------------------------------------------


def test_connect_reports_a_plausible_opus(ftir):
    snapshot = ask(ftir.client.InstrumentConnection.Connect).payload
    assert snapshot["Connected"] is True
    assert snapshot["OpusVersion"] == "9.2"


def test_the_settings_property_settles_on_the_same_opus(ftir):
    """The first read can be the empty starting snapshot, on the real server too.

    The first subscription starts upstream's settings poller and reads at once. If
    the poller holds the executor at that moment, the read gets the cache, which
    starts as OpusInstrumentSnapshot(), connected False. Later reads see the poller's.
    """
    deadline = time.monotonic() + 5
    settings = ftir.client.InstrumentSettings.CurrentInstrumentSettings.get()
    while not settings.Connected and time.monotonic() < deadline:
        time.sleep(0.2)
        settings = ftir.client.InstrumentSettings.CurrentInstrumentSettings.get()
    assert settings.Connected is True
    assert settings.OpusVersion == "9.2"


def test_a_disconnect_is_reported_and_the_next_command_reopens(ftir):
    """The session is persistent now: it stays open between commands until Disconnect."""
    assert ask(ftir.client.InstrumentConnection.Disconnect).payload == {"State": "Disconnected"}
    assert ask(ftir.client.StageControlService.GetStageName, {"Device": ""}).outcome == "Succeeded"


# -- the OPUS function catalogue -----------------------------------------


def test_list_functions_returns_a_catalogue(ftir):
    """Proves the opuspy_api stub satisfies OpusFunctionsProxy's introspection."""
    names = ask(ftir.client.OpusFunctionService.ListFunctions).payload["Names"]
    assert len(names) > 20
    assert "MeasureSample" in names


def test_describe_function_returns_real_parameter_codes(ftir):
    described = ask(
        ftir.client.OpusFunctionService.DescribeFunction, {"FunctionName": "MeasureSample"}
    ).payload
    assert "RES" in described["InputParameters"]
    assert "NSS" in described["InputParameters"]


def test_an_unknown_function_is_rejected(ftir):
    refused = ask(ftir.client.OpusFunctionService.DescribeFunction, {"FunctionName": "NoSuchFunction"})
    assert (refused.outcome, refused.code) == ("Rejected", "opus.unknown_function")


# -- the measurement -----------------------------------------------------


def test_measurement_takes_the_configured_time(ftir):
    t0 = time.monotonic()
    result_of(measure(ftir.client))
    elapsed = time.monotonic() - t0

    # The lower bound is what matters: it catches a mock that returned instantly.
    assert elapsed >= RUN_SECONDS
    assert elapsed < RUN_SECONDS + 10


def test_measurement_streams_its_progress(ftir):
    updates = updates_of(measure(ftir.client))
    assert len(updates) >= 3
    assert {update.outcome for update in updates} == {"Running"}
    percents = [update.payload["Percent"] for update in updates]
    assert percents == sorted(percents), "progress should only go forward"
    assert percents[0] < 50 and percents[-1] == 100


def test_measurement_produces_a_spectrum_and_an_asm_export(ftir):
    result = result_of(measure(ftir.client))

    assert result.outcome == "Succeeded"
    assert [s["BlockName"] for s in result.payload["Spectra"]] == ["AB"]
    assert result.payload["AsmPath"].endswith(".asm.json")
    assert Path(result.payload["AsmPath"]).is_file()


def test_asm_properties_are_filled_from_the_measurement(ftir):
    """The whole chain: fake writes real ASM, SiLA layer reads it back."""
    result = result_of(measure(ftir.client, sample_name="asm-check"))
    measurement_file = result.payload["AsmPath"].rsplit("/", 1)[-1]

    values = {name: getattr(ftir.client.MeasurementService, name).get() for name in ASM_PROPERTIES}

    assert values["DetectorType"] == "DTGS"
    assert values["BeamsplitterType"] == "KBr"
    assert values["EquipmentSerialNumber"] == "MOCK-FTIR-0001"
    assert values["LastSampleIdentifier"] == "asm-check"
    assert values["LastAnalyst"] == "mock"
    assert values["LastMeasurementIdentifier"], "the ASM document should carry a measurement id"
    assert values["LastMeasurementTime"].endswith("Z")
    assert values["LastMeasurementIdentifier"][:8] in measurement_file


def test_the_operator_becomes_the_asm_analyst(ftir):
    result_of(measure(ftir.client, Operator="jw", Project="marvel"))
    assert ftir.client.MeasurementService.LastAnalyst.get() == "jw"


def test_status_returns_to_idle_after_a_measurement(ftir):
    result_of(measure(ftir.client))
    assert ftir.client.InstrumentStatus.CurrentInstrumentStatus.get() == "Idle"


def test_repeated_measurements_each_get_their_own_identifier(ftir):
    first = result_of(measure(ftir.client, sample_name="one"))
    second = result_of(measure(ftir.client, sample_name="two"))
    assert first.payload["AsmPath"] != second.payload["AsmPath"]
    assert ftir.client.MeasurementService.LastSampleIdentifier.get() == "two"


def test_background_measurement_streams_and_succeeds(ftir):
    command = send(
        ftir.client.MeasurementService.MeasureBackground, {"Resolution": 4.0, "BackgroundScans": 16}
    )
    assert updates_of(command), "a background measurement should report progress too"
    assert result_of(command).payload == {"Success": True}


def test_an_existing_file_exports_to_asm_again(ftir):
    measured = result_of(measure(ftir.client, sample_name="first"))
    exported = ask(
        ftir.client.MeasurementService.ExportAsm,
        {
            "FilePath": measured.payload["FilePath"],
            "AsmOutputPath": "",
            "SampleName": "again",
            "Operator": "op",
        },
    )
    assert exported.outcome == "Succeeded"
    assert exported.payload["AsmPath"].endswith(".asm.json")
    assert Path(exported.payload["AsmPath"]).is_file()


# -- the stage and the drawer --------------------------------------------


def test_the_drawer_opens_and_closes(ftir):
    opened = send(ftir.client.StageControlService.OpenDrawer)
    assert result_of(opened).payload == {"Function": "open_drawer", "Response": ["OK"]}
    closed = result_of(send(ftir.client.StageControlService.CloseDrawer))
    assert closed.payload == {"Function": "close_drawer", "Response": ["OK"]}


def test_stage_remembers_where_it_was_moved(ftir):
    assert (
        ask(ftir.client.StageControlService.MoveStage, {"Device": "", "X": 12.5, "Y": -3.0}).outcome
        == "Succeeded"
    )
    position = stage_position(ftir.client)
    assert "X=12.5" in position
    assert "Y=-3.0" in position


# -- the HTS-XT plate stage ----------------------------------------------


# Upstream bug: ftir/sila/ftir_impl.py calls parse_request() in MoveToWell, LoadPlate
# and UnloadPlate but imports only try_parse_request, so on the real server too each
# of the three raises NameError, an UNDEFINED execution error, on every call. The fix
# is one import; these turn green when it lands. MeasureSample's own move to a Well
# does not go through parse_request, so it works today.
PARSE_REQUEST_BUG = pytest.mark.xfail(
    reason="ftir_impl.py uses parse_request without importing it (MoveToWell, LoadPlate, UnloadPlate)",
    strict=True,
)


def a1_and_spacing() -> tuple[int, int, int]:
    config = json.loads((find_instrument_root("ftir") / "hts_xt.json").read_text())
    spacing = int(config["well_spacing_um_by_plate"][str(config["plate_format"])])
    return int(config["a1_x_um"]), int(config["a1_y_um"]), spacing


@PARSE_REQUEST_BUG
def test_move_to_well_uses_the_real_well_geometry(ftir):
    x0, y0, spacing = a1_and_spacing()
    moved = ask(ftir.client.StageControlService.MoveToWell, {"Well": "B3"})
    assert moved.payload["Response"] == ["B3", str(x0 + 2 * spacing), str(y0 + spacing)]
    assert stage_position(ftir.client) == [f"X={float(x0 + 2 * spacing)}", f"Y={float(y0 + spacing)}"]


def test_a_measurement_at_a_well_off_the_plate_fails_before_measuring(ftir):
    t0 = time.monotonic()
    refused = result_of(measure(ftir.client, Well="Z99"))
    assert (refused.outcome, refused.code) == ("Failed", "opus.api")
    assert time.monotonic() - t0 < RUN_SECONDS, "it should fail at the move, not after a measurement"


@PARSE_REQUEST_BUG
def test_load_and_unload_move_the_plate(ftir):
    assert ask(ftir.client.StageControlService.LoadPlate).outcome == "Succeeded"
    assert ask(ftir.client.StageControlService.UnloadPlate).outcome == "Succeeded"


def test_a_measurement_with_a_well_moves_there_first(ftir):
    x0, y0, _ = a1_and_spacing()
    assert result_of(measure(ftir.client, Well="A1")).outcome == "Succeeded"
    assert stage_position(ftir.client) == [f"X={float(x0)}", f"Y={float(y0)}"]
