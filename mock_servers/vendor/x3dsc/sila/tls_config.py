"""Load TLS PEM material from the repo ``cert_tools/`` directory for SiLA servers.

Thin re-export of ``sila_common.tls``; see that module for the implementation.
"""

from __future__ import annotations

from sila_common.tls import TlsMaterial, cert_defaults, load_tls_material, read_pem_file

DEFAULT_CERT_CHAIN, DEFAULT_PRIVATE_KEY = cert_defaults("dsc")

__all__ = [
    "DEFAULT_CERT_CHAIN",
    "DEFAULT_PRIVATE_KEY",
    "TlsMaterial",
    "load_tls_material",
    "read_pem_file",
]
