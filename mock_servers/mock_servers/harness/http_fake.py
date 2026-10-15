"""A fake HTTP layer for the two TRIOS instruments.

Both TriosX3dscClient and TriosSdtClient take a `urlopen_func=` constructor
argument. Injecting there means the mock runs the REAL client: real URL building,
real JSON encoding, real timeout arithmetic, and -- the valuable part -- real error
mapping. Raise an HTTPError from here and it travels the client's own
_decode_response -> _error_details -> TriosClientError -> trios_error_envelope
path and arrives on the wire as the same Rejected or Failed envelope the
instrument would send. A
fake that replaced the whole client would skip all of that.

The shapes here (FakeHeaders / FakeResponse) match what the client's
_decode_response reads: a `.read()` returning bytes and headers exposing
`get_content_type()`.
"""

from __future__ import annotations

import io
import json
import re
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


class FakeHeaders(dict):
    def get_content_type(self) -> str:
        return self.get("Content-Type", "application/octet-stream").split(";", 1)[0]


class FakeResponse:
    def __init__(self, body: bytes, headers: Mapping[str, str] | None = None) -> None:
        self._body = body
        self.headers = FakeHeaders(headers or {"Content-Type": "application/json"})

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


def json_response(payload: Any) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def ok(data: Any = True) -> FakeResponse:
    """The TRIOS success envelope. Every real 2xx body looks like this."""
    return json_response({"success": True, "data": data})


class ApiError(Exception):
    """Raised by a route handler to produce an HTTP error the client will map.

    `can_force` becomes the `canForce` flag the client reads in _error_details and
    surfaces on TriosXxxClientError -- which is how the real API tells a caller
    "this would interrupt a run, pass force=true if you mean it".
    """

    def __init__(self, status: int, message: str, *, can_force: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.can_force = can_force

    def as_http_error(self, url: str) -> HTTPError:
        body = json.dumps(
            {"success": False, "error": {"message": self.message, "canForce": self.can_force}}
        ).encode("utf-8")
        return HTTPError(
            url,
            self.status,
            self.message,
            FakeHeaders({"Content-Type": "application/json"}),
            io.BytesIO(body),
        )


class Request:
    """What a route handler gets: the parsed pieces of one HTTP call."""

    def __init__(self, method: str, path: str, query: dict[str, list[str]], body: Any, url: str) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.url = url
        self.params: dict[str, str] = {}

    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def q_int(self, name: str, default: int | None = None) -> int | None:
        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise ApiError(400, f"{name} must be an integer, got {raw!r}")

    def q_bool(self, name: str, default: bool = False) -> bool:
        raw = self.q(name)
        if raw is None:
            return default
        return raw.lower() in ("1", "true", "yes")

    def json_body(self) -> dict[str, Any]:
        if isinstance(self.body, dict):
            return self.body
        return {}


Handler = Callable[[Request], FakeResponse]

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class _Route:
    def __init__(self, method: str, pattern: str, handler: Handler) -> None:
        self.method = method.upper()
        self.pattern = pattern
        self.handler = handler
        # Escape first so a literal path can never be read as a regex, then swap
        # the escaped placeholders back for named groups.
        literal = re.escape(pattern)
        for name in _PLACEHOLDER.findall(pattern):
            literal = literal.replace(re.escape(f"{{{name}}}"), f"(?P<{name}>[^/]+)")
        self.regex = re.compile(f"^{literal}$")

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        m = self.regex.match(path)
        return m.groupdict() if m else None


class RouteTable:
    """Dispatches a urllib Request to a handler keyed on (method, path).

    Instrument-scoped and common (/api) routes are registered separately because
    the client sends them to different base URLs, and the same tail can mean
    different things under each.
    """

    def __init__(self, instrument_id: str) -> None:
        self.instrument_id = instrument_id
        self._instrument: list[_Route] = []
        self._common: list[_Route] = []
        self._lock = threading.RLock()
        self.calls: list[tuple[str, str]] = []

    # -- registration -----------------------------------------------------

    def instrument(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self._instrument.append(_Route(method, pattern, fn))
            return fn

        return register

    def common(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self._common.append(_Route(method, pattern, fn))
            return fn

        return register

    @contextmanager
    def released(self) -> Iterator[None]:
        """Let other requests in while a handler blocks, as a TRIOS wait does.

        Dispatch holds the table's lock around every handler, which keeps their
        reads and writes of instrument state apart. A wait blocks for as long as a
        run, so it must drop the lock for the wait alone, or every other call
        queues behind it: the status polls that stream its updates, and a stop.
        """
        self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()

    # -- dispatch ---------------------------------------------------------

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        """The urlopen_func the real client calls."""
        url = request.full_url
        method = request.get_method().upper()
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        body = None
        if request.data:
            try:
                body = json.loads(request.data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = None

        path, routes = self._split(parsed.path)
        req = Request(method, path, query, body, url)

        with self._lock:
            self.calls.append((method, path))
            for route in routes:
                params = route.match(method, path)
                if params is not None:
                    req.params = params
                    try:
                        return route.handler(req)
                    except ApiError as e:
                        raise e.as_http_error(url) from None
            raise ApiError(404, f"no mock route for {method} {path}").as_http_error(url)

    def _split(self, full_path: str) -> tuple[str, list[_Route]]:
        """Strip whichever base prefix the client used, and say which table to search."""
        instrument_prefix = f"/api/v1/instruments/{self.instrument_id}"
        if full_path.startswith(instrument_prefix):
            return full_path[len(instrument_prefix) :] or "/", self._instrument
        if full_path.startswith("/api"):
            return full_path[len("/api") :] or "/", self._common
        return full_path, self._instrument

    def call_count(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p in self.calls if m == method.upper() and p == path)
