"""A stand-in for Bruker's Windows-only `opuspy_api` wheel.

This has to be installed into sys.modules BEFORE `import sila.server`, and the
reason is not obvious: `import sila.server` itself succeeds without opuspy_api,
because ftir_automation imports it lazily. It is *registration* that fails.
register_ftir_features constructs OpusFunctionServiceImpl, whose __init__
(ftir/sila/ftir_impl.py:836) calls _metadata_proxy(), which at line 318 imports
opuspy_api.opus_commands.generated.opus_functions.

OpusFunctionsProxy.list_functions() builds its catalogue from dir(type(api)),
dropping names that start with "_" or end with "_parameters", so the catalogue
below is what DescribeFunction and ListFunctions will report.
"""

from __future__ import annotations

import sys
import types


class OpusAPIError(Exception):
    """Mirrors opuspy_api.opus_commands.errors.OpusAPIError.

    ftir_automation/core.py:557 imports this lazily to recognise OPUS failures,
    so it must exist and be an Exception subclass.
    """


def _returns_ok(*_args, **_kwargs):
    return ["OK"]


# A small but plausible slice of OPUS's 401 functions, with the parameter codes
# the real ones use. Enough for ListFunctions and DescribeFunction to look real
# without pretending to be the full catalogue.
_FUNCTIONS: dict[str, tuple[list[str], list[str]]] = {
    "MeasureSample": (["RES", "NSS", "APT", "VEL", "NAM", "SNM"], ["FILE"]),
    "MeasureReference": (["RES", "NSR", "APT", "VEL"], ["OK"]),
    "LoadFile": (["PTH"], ["FILE"]),
    "UnloadFile": (["FILE"], ["OK"]),
    "SaveAs": (["FILE", "PTH", "NAM", "SAN"], ["OK"]),
    "Baseline": (["FILE", "BLM"], ["FILE"]),
    "PeakPick": (["FILE", "PPM", "PSM"], ["PEAKS"]),
    "Integrate": (["FILE", "INM"], ["RESULT"]),
    "AtmosphericCompensation": (["FILE"], ["FILE"]),
    "Normalize": (["FILE", "NMM"], ["FILE"]),
    "Smooth": (["FILE", "SMP"], ["FILE"]),
    "Subtract": (["FILE", "FILE2", "SUF"], ["FILE"]),
    "Derivative": (["FILE", "DEP"], ["FILE"]),
    "ConvertToAbsorbance": (["FILE"], ["FILE"]),
    "ConvertToTransmittance": (["FILE"], ["FILE"]),
    "CutSpectrum": (["FILE", "FXP", "LXP"], ["FILE"]),
    "GetParameter": (["FILE", "PAR"], ["VALUE"]),
    "SetParameter": (["FILE", "PAR", "VALUE"], ["OK"]),
    "OpenDrawer": ([], ["OK"]),
    "CloseDrawer": ([], ["OK"]),
    "MoveStage": (["XPO", "YPO"], ["OK"]),
    "GetStagePosition": ([], ["XPO", "YPO"]),
    "GetStageName": ([], ["NAME"]),
    "GetNumberOfAxes": ([], ["AXES"]),
    "SetReferencePosition": (["XPO", "YPO"], ["OK"]),
    "IsReferencePlateAvailable": ([], ["AVAILABLE"]),
    "SendCommand": (["CMD"], ["RESPONSE"]),
    "GetVersion": ([], ["VERSION"]),
    "ShowOpus": ([], ["OK"]),
    "HideOpus": ([], ["OK"]),
}


def _build_opus_functions_class() -> type:
    """Build the class OpusFunctionsProxy will introspect."""
    namespace: dict[str, object] = {
        "__doc__": "Mock OPUS function catalogue (mock_servers).",
    }
    for name, (inputs, returns) in _FUNCTIONS.items():
        namespace[name] = _returns_ok
        namespace[f"{name}_parameters"] = (
            lambda _self=None, _i=inputs, _r=returns: (list(_i), list(_r))
        )
    return type("OpusFunctions", (), namespace)


OpusFunctions = _build_opus_functions_class()


class OpusAPI:
    """Stands in for opuspy_api.OpusAPI. The mock never calls through it."""

    def __init__(self, *args, **kwargs) -> None:
        self.opus_functions = OpusFunctions()

    def close(self) -> None:
        pass


def install() -> None:
    """Register the whole opuspy_api module tree. Idempotent."""
    if "opuspy_api" in sys.modules:
        return

    root = types.ModuleType("opuspy_api")
    root.__path__ = []
    root.OpusAPI = OpusAPI

    commands = types.ModuleType("opuspy_api.opus_commands")
    commands.__path__ = []

    generated = types.ModuleType("opuspy_api.opus_commands.generated")
    generated.__path__ = []

    functions = types.ModuleType("opuspy_api.opus_commands.generated.opus_functions")
    functions.OpusFunctions = OpusFunctions

    errors = types.ModuleType("opuspy_api.opus_commands.errors")
    errors.OpusAPIError = OpusAPIError

    sys.modules.update(
        {
            "opuspy_api": root,
            "opuspy_api.opus_commands": commands,
            "opuspy_api.opus_commands.generated": generated,
            "opuspy_api.opus_commands.generated.opus_functions": functions,
            "opuspy_api.opus_commands.errors": errors,
        }
    )
