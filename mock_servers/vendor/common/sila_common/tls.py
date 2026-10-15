"""Load TLS PEM material from the repo ``cert_tools/`` directory for SiLA servers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

CERT_TOOLS_ENV = "SILA_CERT_TOOLS"


class TlsMaterial(NamedTuple):
    private_key: bytes
    cert_chain: bytes
    ca_for_discovery: bytes | None


def _walk_for_cert_tools(start: Path) -> Path | None:
    here = start if start.is_dir() else start.parent
    for candidate in (here, *here.parents):
        if candidate.name == "cert_tools" and candidate.is_dir():
            return candidate.resolve()
        found = candidate / "cert_tools"
        if found.is_dir():
            return found.resolve()
    return None


def cert_tools_dir() -> Path:
    """Return the directory that holds SiLA PEM files.

    Resolution order:

    1. ``SILA_CERT_TOOLS`` environment variable (directory path)
    2. A ``cert_tools/`` directory found by walking parents of the cwd
    3. A ``cert_tools/`` directory found by walking parents of this file
       (works for both an editable checkout and a venv inside the repo)
    4. ``<cwd>/cert_tools`` as a last-resort path (may not exist yet)
    """
    env = os.environ.get(CERT_TOOLS_ENV, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    found = _walk_for_cert_tools(Path.cwd()) or _walk_for_cert_tools(Path(__file__).resolve())
    if found is not None:
        return found
    return (Path.cwd() / "cert_tools").resolve()


def cert_defaults(instrument_prefix: str) -> tuple[Path, Path]:
    """Return ``(cert_chain, private_key)`` paths under ``cert_tools/``.

    *instrument_prefix* is the filename stem without ``_server``, e.g.
    ``"ftir"``, ``"sdt"``, ``"dsc"``.
    """
    root = cert_tools_dir()
    chain = root / f"{instrument_prefix}_server.crt"
    key = root / f"{instrument_prefix}_server.key"
    return chain, key


def read_pem_file(path: str | Path, *, example_key: str | None = None) -> bytes:
    """Read a PEM file and reject placeholder / empty content."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"TLS file not found: {p}\n"
            f"Generate certs under cert_tools/ (see cert_tools/README.md), "
            f"set {CERT_TOOLS_ENV}, or pass --insecure."
        )
    data = p.read_bytes()
    stripped = data.lstrip()
    if not stripped or stripped.startswith(b"#"):
        key_hint = example_key or p.with_suffix(".key").name
        crt_hint = p.with_suffix(".crt").name if p.suffix != ".crt" else p.name
        if example_key:
            crt_hint = example_key.replace(".key", ".crt")
        raise ValueError(
            f"TLS file {p} is not valid PEM data (placeholder or empty).\n"
            f"Generate real certs from the repo root, e.g.:\n"
            f"  openssl req -x509 -nodes -days 365 -newkey rsa:2048 "
            f"-keyout cert_tools/{key_hint} -out cert_tools/{crt_hint} "
            f"-config cert_tools/openssl.cnf"
        )
    if b"-----BEGIN" not in stripped:
        raise ValueError(f"TLS file {p} does not look like PEM (missing -----BEGIN).")
    return data


def load_tls_material(
    *,
    private_key_path: str | Path,
    cert_chain_path: str | Path,
    ca_for_discovery_path: str | Path | None = None,
    example_key: str | None = None,
) -> TlsMaterial:
    """Load server TLS key/cert (and optional CA for discovery) from disk."""
    private_key = read_pem_file(private_key_path, example_key=example_key)
    cert_chain = read_pem_file(cert_chain_path, example_key=example_key)
    ca: bytes | None = None
    if ca_for_discovery_path is not None:
        ca = read_pem_file(ca_for_discovery_path, example_key=example_key)
    return TlsMaterial(
        private_key=private_key,
        cert_chain=cert_chain,
        ca_for_discovery=ca,
    )


def __getattr__(name: str) -> Path:
    if name == "DEFAULT_CERT_TOOLS":
        return cert_tools_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
