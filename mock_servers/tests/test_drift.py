"""Catches the mock falling behind the real instrument servers.

This is the most valuable test here, because drift is exactly how the previous
mock failed. backend/docs/BACKLOG.md records it: a hand-copied X3 mock "kept
agreeing with gateway code that the real instrument would reject -- the previous
snapshot still had ControlStart unobservable and parameterless months after it
became an observable three-parameter command."

The seam already makes that specific failure impossible: the mocks import the real
register_*_features and the real generated feature classes, so the SiLA surface IS
the real surface. Two things can still drift, and each gets a check here:

1. The feature definitions change (a new command, a new parameter). The mock keeps
   serving them correctly, but its fake driver may not implement what they need.
2. A client method gains a route, or an impl calls an automation method, that the
   fake has no answer for. That would only show up at runtime, on whichever
   command happens to hit it.

Both checks are static -- no server, no client, no protobuf. They run in
milliseconds and are the first thing to look at when something behaves oddly.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from mock_servers.harness.instrument_root import find_instrument_root

EXPECTED = Path(__file__).parent / "expected"

# What each instrument's sila/features/ directory should contain. A count is a
# weak check on its own; the fingerprints below are the real one.
FEATURE_COUNTS = {"x3dsc": 12, "sdt": 15, "gpc": 5, "ftir": 6}


def fdl_fingerprints(root: Path) -> dict[str, str]:
    """sha256 per feature definition file, keyed by feature name."""
    return {
        path.name.replace(".sila.xml", ""): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "sila" / "features").glob("*.sila.xml"))
    }


@pytest.mark.parametrize("instrument", sorted(FEATURE_COUNTS))
def test_feature_count_is_unchanged(instrument):
    root = find_instrument_root(instrument)
    features = sorted((root / "sila" / "features").glob("*.sila.xml"))
    assert len(features) == FEATURE_COUNTS[instrument], (
        f"{instrument} now has {len(features)} features, expected "
        f"{FEATURE_COUNTS[instrument]}. If a feature was genuinely added or removed, "
        f"update FEATURE_COUNTS here and the count assertion in the matching e2e test."
    )


@pytest.mark.parametrize("instrument", sorted(FEATURE_COUNTS))
def test_feature_definitions_are_unchanged(instrument):
    """Fires the moment marvel_digitalisation regenerates a feature definition.

    A changed hash is not automatically a failure of anything -- it is a prompt to
    check whether the fake driver still satisfies the new definition, then refresh
    the snapshot with: python -m tests.refresh_fingerprints
    """
    root = find_instrument_root(instrument)
    snapshot_path = EXPECTED / f"{instrument}_fdl.json"
    if not snapshot_path.exists():
        pytest.fail(
            f"no fingerprint snapshot for {instrument}. Create it with: "
            f"python -m tests.refresh_fingerprints"
        )

    expected = json.loads(snapshot_path.read_text())
    actual = fdl_fingerprints(root)

    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k])

    assert not (added or removed or changed), (
        f"{instrument} feature definitions moved.\n"
        f"  added:   {added}\n"
        f"  removed: {removed}\n"
        f"  changed: {changed}\n"
        f"Check the fake driver still satisfies them, then refresh with: "
        f"python -m tests.refresh_fingerprints"
    )


# -- route coverage for the two TRIOS instruments ------------------------


def client_routes(client_path: Path) -> set[tuple[str, str]]:
    """Every (method, path) the real REST client can issue, read from its source."""
    tree = ast.parse(client_path.read_text())
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "_request" or len(node.args) < 2:
            continue
        method_node, path_node = node.args[0], node.args[1]
        if not isinstance(method_node, ast.Constant):
            continue
        if isinstance(path_node, ast.Constant):
            path = path_node.value
        elif isinstance(path_node, ast.JoinedStr):
            # f"/queue/experiments/{quote(id)}" -> "/queue/experiments/{}"
            path = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in path_node.values
            )
        else:
            continue
        routes.add((method_node.value, path))
    return routes


_NAMED_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def mock_routes(table) -> set[tuple[str, str]]:
    """Every (method, path) the mock's route table answers, in the same shape."""
    return {
        # "/queue/experiments/{experimentId}" -> "/queue/experiments/{}", matching
        # how client_routes() renders an f-string path.
        (route.method, _NAMED_PLACEHOLDER.sub("{}", route.pattern))
        for route in (*table._instrument, *table._common)
    }


@pytest.mark.parametrize("instrument", ["x3dsc", "sdt"])
def test_every_client_route_has_a_mock_handler(instrument):
    """The check that would have caught a missing route before runtime.

    An unhandled route 404s, and because the backend's PropertyMonitor reads every
    property on a cycle, that becomes a permanently failing tile on the dashboard
    rather than an obvious error.
    """
    root = find_instrument_root(instrument)
    if instrument == "x3dsc":
        from mock_servers.x3dsc.instrument import X3DscInstrument, build_routes

        table = build_routes(X3DscInstrument(run_seconds=5.0), "TEST")
        client_source = root / "x3dsc_automation" / "trios_x3dsc_client.py"
    else:
        from mock_servers.sdt.instrument import SdtInstrument, build_routes

        table = build_routes(SdtInstrument(run_seconds=5.0), "TEST")
        client_source = root / "sdt_automation" / "trios_sdt_client.py"

    missing = client_routes(client_source) - mock_routes(table)
    assert not missing, (
        f"{instrument}: the real client can call routes the mock does not answer: "
        f"{sorted(missing)}"
    )


# -- driver coverage for the two duck-typed instruments ------------------


def attributes_reached_on(source: Path, owner: str) -> set[str]:
    """Every `<something>.<owner>.<name>` the impl module touches."""
    tree = ast.parse(source.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == owner
        ):
            names.add(node.attr)
    return names


def test_fake_gpc_automation_implements_everything_the_impls_call():
    root = find_instrument_root("gpc")
    from mock_servers.gpc.fake_automation import FakeGpcAutomation

    needed = attributes_reached_on(root / "sila" / "gpc_impl.py", "gpc")
    fake = FakeGpcAutomation(run_seconds=5.0)
    missing = sorted(name for name in needed if not hasattr(fake, name))

    assert not missing, (
        f"gpc_impl.py calls these on the automation object, and the fake has no "
        f"answer for them: {missing}"
    )


def test_fake_ftir_automation_implements_everything_the_impls_call():
    root = find_instrument_root("ftir")
    from mock_servers.ftir.fake_automation import FakeFtirAutomation

    # FtirExecutor moved out of ftir_impl.py into executor.py, and it reaches into
    # the automation object too (_opus_api, _opus_ip, connect, ...).
    needed = attributes_reached_on(root / "sila" / "ftir_impl.py", "ftir") | attributes_reached_on(
        root / "sila" / "executor.py", "ftir"
    )
    fake = FakeFtirAutomation(run_seconds=5.0)
    try:
        missing = sorted(name for name in needed if not hasattr(fake, name))
    finally:
        fake.close()

    assert not missing, (
        f"ftir_impl.py or executor.py reaches for these on the automation object, and the fake has "
        f"no answer for them: {missing}"
    )


def test_fake_ftir_stage_implements_every_named_stage_method():
    """StageControlService reaches methods by name, so a typo only shows at runtime."""
    root = find_instrument_root("ftir")
    from mock_servers.ftir.fake_automation import FakeStageCommands

    tree = ast.parse((root / "sila" / "ftir_impl.py").read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_run_stage"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            called.add(node.args[0].value)

    assert called, "found no _run_stage calls; the extraction is wrong, not the fake"
    stage = FakeStageCommands()
    missing = sorted(name for name in called if not hasattr(stage, name))
    assert not missing, f"StageControlService calls stage methods the fake lacks: {missing}"
