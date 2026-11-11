"""Readiness and equipment status HTTP server. Handlers only read a cache."""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from argparse import ArgumentParser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log = logging.getLogger(__name__)


class _StatusHTTPServer(ThreadingHTTPServer):
    """Refuse a second bind. ``HTTPServer`` sets ``SO_REUSEADDR``, which on Windows does not."""

    allow_reuse_address = False


class ReachabilityCache:
    """Last finished reachability sample. ``None`` means no sample yet."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._up: bool | None = None
        self._kind: str | None = None
        self._parameters: dict = {}
        self._activity: str | None = None
        self._sampled_at: str | None = None

    def configure(self, kind: str, parameters: dict[str, tuple[str, str]]) -> None:
        """Set the public parameter schema before starting the HTTP server."""
        with self._lock:
            self._kind = kind
            self._parameters = {
                key: {"label": label, "unit": unit, "value": None}
                for key, (label, unit) in parameters.items()
            }

    def record_parameters(self, values: dict, activity: str | None) -> None:
        with self._lock:
            for key, parameter in self._parameters.items():
                parameter["value"] = values.get(key)
            self._activity = activity
            self._sampled_at = datetime.now(timezone.utc).isoformat()

    def record_activity(self, activity: str) -> None:
        with self._lock:
            self._activity = activity

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy({
                "kind": self._kind,
                "up": self._up is True,
                "activity": self._activity,
                "sampled_at": self._sampled_at,
                "parameters": self._parameters,
            })

    def record(self, up: bool) -> None:
        with self._lock:
            self._up = bool(up)
            if not up:
                for parameter in self._parameters.values():
                    parameter["value"] = None
                self._activity = "Unreachable"
                self._sampled_at = datetime.now(timezone.utc).isoformat()
            if self._kind == "ftir":
                self._parameters["instrument_up"]["value"] = bool(up)
                self._sampled_at = datetime.now(timezone.utc).isoformat()

    def skip(self) -> None:
        """Leave the previous sample unchanged."""

    def get(self) -> bool | None:
        with self._lock:
            return self._up


class StatusHttpServer:
    """Serve readiness at /health and the latest equipment snapshot at /status."""

    def __init__(self, cache: ReachabilityCache, host: str, port: int) -> None:
        self._cache = cache
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._port
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler = _handler(self._cache)
        try:
            self._httpd = _StatusHTTPServer((self._host, self._port), handler)
        except OSError:
            _log.exception("status HTTP server failed to bind %s:%s", self._host, self._port)
            raise
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="status-http",
            daemon=True,
        )
        self._thread.start()
        _log.info("status HTTP server listening on %s:%s", self._host, self.port)

    def stop(self, timeout: float = 5.0) -> None:
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=timeout)


class CachingHealthPoller:
    """Background probe that only writes :class:`ReachabilityCache`."""

    def __init__(
        self,
        cache: ReachabilityCache,
        probe: Callable[[], bool],
        *,
        interval_s: float,
        name: str,
    ) -> None:
        self._cache = cache
        self._probe_fn = probe
        self.interval_s = interval_s
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def probe(self) -> bool:
        try:
            up = bool(self._probe_fn())
        except Exception:
            self._cache.record(False)
            raise
        self._cache.record(up)
        return up

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        _log.info("Started %s (interval=%.0fs)", self.name, self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.probe()
            except Exception:
                _log.exception("%s probe failed", self.name)
            if self._stop.wait(self.interval_s):
                break


def add_status_args(parser: ArgumentParser, *, port_env: str) -> None:
    """Add readiness flags. Port env is per instrument; interval and timeout are shared."""
    parser.add_argument(
        "--status-port",
        type=int,
        default=_env_int(port_env, 8081),
        help=f"Readiness HTTP port (default {port_env} or 8081).",
    )
    parser.add_argument(
        "--status-poll-interval",
        type=float,
        default=_env_float("STATUS_POLL_INTERVAL_S", 15.0),
        help="Seconds between reachability probes (default STATUS_POLL_INTERVAL_S or 15).",
    )
    parser.add_argument(
        "--status-probe-timeout",
        type=float,
        default=_env_float("STATUS_PROBE_TIMEOUT_S", 3.0),
        help="Seconds a probe may block before the cache is left unchanged "
        "(default STATUS_PROBE_TIMEOUT_S or 3).",
    )


def _handler(cache: ReachabilityCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/health", "/status"):
                self.send_error(404)
                return
            up = cache.get() is True
            body = json.dumps(cache.snapshot() if path == "/status" else {"up": up}).encode()
            self.send_response(200 if path == "/status" or up else 503)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(404)

        do_PUT = do_DELETE = do_HEAD = do_PATCH = do_POST  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
