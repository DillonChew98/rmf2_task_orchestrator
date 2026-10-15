"""Load TLS PEM material from the repo ``cert_tools/`` directory for SiLA servers."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

# automation/gpc/sila/tls_config.py -> automation/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CERT_CHAIN = _REPO_ROOT / "cert_tools" / "gpc_server.crt"
DEFAULT_PRIVATE_KEY = _REPO_ROOT / "cert_tools" / "gpc_server.key"


class TlsMaterial(NamedTuple):
    private_key: bytes
    cert_chain: bytes
    ca_for_discovery: bytes | None


def read_pem_file(path: str | Path) -> bytes:
    """Read a PEM file and reject placeholder / empty content."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"TLS file not found: {p}\n"
            f"Generate certs under cert_tools/ (see cert_tools/README.md) or pass --insecure."
        )
    data = p.read_bytes()
    stripped = data.lstrip()
    if not stripped or stripped.startswith(b"#"):
        raise ValueError(
            f"TLS file {p} is not valid PEM data (placeholder or empty).\n"
            f"Generate real certs from the repo root, e.g.:\n"
            f"  openssl req -x509 -nodes -days 365 -newkey rsa:2048 "
            f"-keyout cert_tools/gpc_server.key -out cert_tools/gpc_server.crt -config cert_tools/openssl.cnf"
        )
    if b"-----BEGIN" not in stripped:
        raise ValueError(f"TLS file {p} does not look like PEM (missing -----BEGIN).")
    return data


def load_tls_material(
    *,
    private_key_path: str | Path,
    cert_chain_path: str | Path,
    ca_for_discovery_path: str | Path | None = None,
) -> TlsMaterial:
    """Load server TLS key/cert (and optional CA for discovery) from disk."""
    private_key = read_pem_file(private_key_path)
    cert_chain = read_pem_file(cert_chain_path)
    ca: bytes | None = None
    if ca_for_discovery_path is not None:
        ca = read_pem_file(ca_for_discovery_path)
    return TlsMaterial(
        private_key=private_key,
        cert_chain=cert_chain,
        ca_for_discovery=ca,
    )
