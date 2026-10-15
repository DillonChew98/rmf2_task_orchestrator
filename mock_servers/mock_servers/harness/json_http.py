"""A small JSON-over-HTTP server for the two robot mocks, on the standard library only.

Each mock supplies one function, Request -> (status, body). All of HTTP lives
here. The log line for a call holds its method, path and status, and never a
header or the query, so the MiR login that the orchestrator's template sends
never reaches a log.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("mock_servers.http")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    body: object

    def arg(self, name: str) -> str | None:
        """The last value of one query parameter, "" when it is present but empty."""
        values = self.query.get(name)
        return values[-1] if values else None


App = Callable[[Request], tuple[int, object]]


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1, because the orchestrator's client keeps connections open. Every
    # answer then has to say where it ends, which _answer does.
    protocol_version = "HTTP/1.1"
    # Drops a kept-open connection that has gone quiet.
    timeout = 60

    def do_GET(self) -> None:
        self._answer()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def _answer(self) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            status, payload = 400, {"error_human": "the body is not JSON"}
        else:
            request = Request(self.command, path, parse_qs(url.query, keep_blank_values=True), body)
            try:
                status, payload = self.server.app(request)
            except Exception:
                logger.exception("%s %s failed", self.command, path)
                status, payload = 500, {"error_human": "the mock failed, see its log"}
        data = b"" if payload is None else json.dumps(payload).encode()
        logger.info("%s %s %d", self.command, path, status)
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-") -> None:
        """Silenced: _answer logs each call itself, without the query."""

    def log_message(self, format: str, *args) -> None:
        logger.debug(format, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        self.app = app
        super().__init__(address, _Handler)


def bind(host: str, port: int, app: App) -> ThreadingHTTPServer:
    try:
        return _Server((host, port), app)
    except OSError as error:
        raise SystemExit(
            f"cannot listen on {host}:{port}: {error.strerror}. Is another mock already running there?"
        ) from None


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def serve_until_stopped(server: ThreadingHTTPServer, banner: str) -> None:
    """Serve until Ctrl+C or SIGTERM, so a launcher script can stop it cleanly."""
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, _frame: stopping.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("%s", banner)
    try:
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        logger.info("interrupted, stopping")
    finally:
        server.shutdown()
        server.server_close()
        logger.info("stopped")
