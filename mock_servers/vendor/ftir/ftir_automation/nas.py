"""Optional SFTP upload of MeasureSample files to the lab NAS."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

_log = logging.getLogger(__name__)

_DEFAULT_HOST = "192.168.68.150"
_DEFAULT_DIR = "/volume1/polymer_dev/ftir"
_DEFAULT_PORT = 22


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def nas_sftp_configured() -> bool:
    """True when host, user, and password are all set."""
    return bool(
        _env("FTIR_NAS_SFTP_HOST", _DEFAULT_HOST)
        and _env("FTIR_NAS_SFTP_USER")
        and _env("FTIR_NAS_SFTP_PASSWORD")
    )


def _result_path(result: Any, attr: str) -> str:
    return str(getattr(result, attr, "") or "").strip()


def _measurement_local_paths(result: Any) -> list[str]:
    """Collect existing local files produced by a successful measurement."""
    file_path = _result_path(result, "file_path")
    candidates = [
        file_path,
        _result_path(result, "asm_path"),
    ]
    if file_path:
        try:
            from ftir_automation.peaks import processed_json_path

            candidates.append(processed_json_path(file_path=file_path))
        except Exception:
            pass

    paths: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        path = str(path).strip()
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _ensure_remote_dir(sftp: Any, remote_dir: str) -> None:
    """Create *remote_dir* and parents when missing (POSIX paths)."""
    current = ""
    for part in remote_dir.strip("/").split("/"):
        if not part:
            continue
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def _open_ssh_client(
    *, host: str, port: int, username: str, password: str
) -> Any:
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    return client


def upload_paths(
    local_paths: list[str],
    *,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    remote_dir: str | None = None,
    connect: Callable[..., Any] | None = None,
) -> int:
    """Upload *local_paths* to the NAS over SFTP. Returns the upload count.

    Raises on connection failure and on any ``put`` failure. Files already
    uploaded stay on the share.
    """
    if host is None:
        host = _env("FTIR_NAS_SFTP_HOST", _DEFAULT_HOST)
    if username is None:
        username = _env("FTIR_NAS_SFTP_USER")
    if password is None:
        password = _env("FTIR_NAS_SFTP_PASSWORD")
    if remote_dir is None:
        remote_dir = _env("FTIR_NAS_SFTP_DIR", _DEFAULT_DIR)
    remote_dir = remote_dir.rstrip("/")
    if port is None:
        raw_port = _env("FTIR_NAS_SFTP_PORT", str(_DEFAULT_PORT))
        port = int(raw_port) if raw_port else _DEFAULT_PORT

    if not host or not username or not password or not local_paths:
        return 0

    open_client = connect or (
        lambda: _open_ssh_client(
            host=host, port=port, username=username, password=password
        )
    )
    client = open_client()
    uploaded = 0
    try:
        sftp = client.open_sftp()
        try:
            _ensure_remote_dir(sftp, remote_dir)
            for local_path in local_paths:
                remote_path = f"{remote_dir}/{os.path.basename(local_path)}"
                try:
                    sftp.put(local_path, remote_path)
                    uploaded += 1
                    _log.info("NAS uploaded %s -> %s", local_path, remote_path)
                except Exception:
                    _log.exception(
                        "NAS upload failed for %s -> %s", local_path, remote_path
                    )
                    raise
        finally:
            sftp.close()
    finally:
        client.close()
    return uploaded


def maybe_upload_measurement(result: Any) -> int:
    """Upload MeasureSample files to the NAS when configured.

    Returns the number of files uploaded. Missing credentials or no local
    files skip the upload. A configured upload that fails raises.
    """
    if not getattr(result, "success", False):
        return 0
    if not nas_sftp_configured():
        _log.debug("NAS SFTP skipped: credentials unset")
        return 0
    paths = _measurement_local_paths(result)
    if not paths:
        return 0
    return upload_paths(paths)
