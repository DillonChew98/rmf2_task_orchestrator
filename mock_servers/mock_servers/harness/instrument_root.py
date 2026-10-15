"""Finding one instrument's packages, and putting them on sys.path.

The real instrument packages are copied into vendor/ by
scripts/vendor_instruments.sh. The mocks do not install them. Every instrument
has a top-level package literally named `sila`, so installing two into one
environment would have the second shadow the first. Instead each mock process
inserts exactly one instrument directory onto sys.path and imports `sila.server`
from there. Separate processes never share a sys.path, so the clash cannot happen.

Set MARVEL_DIGITALISATION_ROOT, or pass --instrument-root, to run against a live
marvel_digitalisation checkout instead of vendor/.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

INSTRUMENTS = ("x3dsc", "sdt", "gpc", "ftir")

_ENV_VAR = "MARVEL_DIGITALISATION_ROOT"
_VENDOR = Path(__file__).resolve().parents[2] / "vendor"


class InstrumentRootNotFound(RuntimeError):
    pass


def _candidate_roots(explicit: str | Path | None) -> list[tuple[str, Path]]:
    """Every place we would look, best first, each labelled for the error message."""
    candidates: list[tuple[str, Path]] = []
    if explicit is not None:
        candidates.append(("--instrument-root", Path(explicit)))
    env = os.environ.get(_ENV_VAR)
    if env:
        candidates.append((f"${_ENV_VAR}", Path(env)))
    candidates.append(("vendor/", _VENDOR))
    return candidates


def find_instrument_root(name: str, explicit: str | Path | None = None) -> Path:
    """Locate one instrument's package directory, or fail saying where we looked.

    Never falls back silently. An unexplained ModuleNotFoundError from a missing
    vendor/ is an hour nobody gets back.
    """
    if name not in INSTRUMENTS:
        raise ValueError(f"unknown instrument {name!r}; expected one of {INSTRUMENTS}")

    tried: list[str] = []
    for label, root in _candidate_roots(explicit):
        # An explicit path may point at either the root holding all instruments or
        # the instrument directory itself. Accept both; the mistake is easy to make.
        for path in (root / name, root):
            if (path / "sila" / "server.py").is_file():
                return path.resolve()
            tried.append(f"  {label}: {path}")

    raise InstrumentRootNotFound(
        f"could not find the {name!r} instrument packages.\n"
        f"Looked for <path>/sila/server.py at:\n" + "\n".join(tried) + "\n"
        f"Fix by running scripts/vendor_instruments.sh, setting {_ENV_VAR}, "
        f"or passing --instrument-root."
    )


def add_common_to_sys_path() -> None:
    """Make the vendored `sila_common` and `trios_common` importable."""
    _insert(_VENDOR / "common")


def add_to_sys_path(root: Path) -> None:
    """Put one instrument's packages first on sys.path.

    Must run before `import sila.server`. Only ever call this once per process.
    `sila_common` and `trios_common` come along: from vendor/common when the
    root is vendored, otherwise from whatever the environment has installed.
    """
    _insert(root)
    add_common_to_sys_path()


def _insert(path: Path) -> None:
    entry = str(path)
    if entry in sys.path or not path.is_dir():
        return
    sys.path.insert(0, entry)
