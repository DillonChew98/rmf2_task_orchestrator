"""HTTP test doubles shared by SDT / X3DSC client and SiLA server tests."""

from __future__ import annotations

from typing import Any


class FakeHeaders(dict):
    def get_content_type(self) -> str:
        return self.get("Content-Type", "application/octet-stream").split(";", 1)[0]


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = FakeHeaders(headers or {"Content-Type": "application/json"})

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class RecordingUrlOpen:
    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse(b'{"ok": true}')
        self.error = error
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: Any, timeout: Any = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


class SequencedUrlOpen:
    """Return a sequence of responses/errors for successive HTTP calls."""

    def __init__(self, *outcomes: FakeResponse | Exception) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: Any, timeout: Any = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._outcomes:
            raise AssertionError("No more sequenced HTTP outcomes")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UrlRoutingOpen:
    """Route by URL substring to a fixed FakeResponse or Exception."""

    def __init__(self, routes: dict[str, FakeResponse | Exception]) -> None:
        self.routes = routes
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: Any, timeout: Any = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = getattr(request, "full_url", str(request))
        for needle, outcome in self.routes.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"No route matched URL: {url}")
