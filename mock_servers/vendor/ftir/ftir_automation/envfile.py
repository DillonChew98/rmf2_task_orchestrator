"""Minimal ``.env`` loader (no third-party dependency).

Loads ``KEY=VALUE`` lines into ``os.environ`` without overriding keys that are
already set. Used by the SiLA server entry point so lab PCs can keep OTLP,
database, and NAS settings in ``ftir/.env``.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """Load *path* into the process environment.

    Returns the path that was loaded, or ``None`` if the file is missing.
    """
    if path is None:
        # sila/ -> ftir package root (where pyproject.toml / .env live)
        path = Path(__file__).resolve().parents[1] / ".env"
    else:
        path = Path(path)
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return path


def ftir_output_dir() -> str:
    """Return ``FTIR_OUTPUT_DIR`` with quotes and a trailing separator removed."""
    raw = os.environ.get("FTIR_OUTPUT_DIR", "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    return raw.rstrip("\\/")
