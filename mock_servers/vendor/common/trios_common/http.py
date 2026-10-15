"""Shared urllib transport for TRIOS instrument REST clients."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

JsonValue = Any
JsonMapping = Mapping[str, JsonValue]
UrlOpenCallable = Callable[..., Any]

WAIT_TIMEOUT_MARGIN_SECONDS = 15.0
DEFAULT_WAIT_UNTIL_IDLE_SECONDS = 120
DEFAULT_WAIT_FOR_TEST_STARTED_SECONDS = 30
DEFAULT_CONTROL_START_TIMEOUT_SECONDS = 120


def coerce_query_value(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def content_type(headers: Any) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        return getter().strip().lower()
    value = headers.get("Content-Type", "")
    return value.split(";", 1)[0].strip().lower()


def decode_response(body: bytes, headers: Any) -> JsonValue | None:
    if not body:
        return None

    text = body.decode("utf-8")
    ctype = content_type(headers)
    if ctype == "application/json":
        return json.loads(text)

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(text)

    return text


def error_details(error_payload: JsonValue, fallback: str) -> tuple[str, bool]:
    can_force = False
    message = fallback
    if not isinstance(error_payload, Mapping):
        return message, can_force

    nested = error_payload.get("error")
    if isinstance(nested, Mapping):
        message = str(nested.get("message") or message)
        can_force = bool(nested.get("canForce"))
        return message, can_force

    message = str(
        error_payload.get("message")
        or error_payload.get("detail")
        or error_payload.get("error")
        or message
    )
    can_force = bool(error_payload.get("canForce"))
    return message, can_force


@dataclass(slots=True)
class TriosClientError(RuntimeError):
    message: str
    status_code: int | None = None
    url: str | None = None
    payload: JsonValue | None = None
    can_force: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def __str__(self) -> str:
        details: list[str] = [self.message]
        if self.status_code is not None:
            details.append(f"status={self.status_code}")
        if self.url:
            details.append(f"url={self.url}")
        if self.can_force:
            details.append("canForce=true")
        return " ".join(details)


class TriosHttpClient:
    """Synchronous TRIOS REST transport (instrument + common ``/api`` routes)."""

    #: Fallback text when HTTPError.reason is empty.
    api_error_fallback: str = "TRIOS API request failed"

    def __init__(
        self,
        api_base_url: str,
        instrument_id: str | None = None,
        *,
        bearer_token: str | None = None,
        timeout: float = 30.0,
        urlopen_func: UrlOpenCallable | None = None,
    ) -> None:
        self.instrument_base_url = self._normalize_base_url(api_base_url, instrument_id)
        self.common_api_base_url = self._derive_common_api_base(api_base_url)
        self.instrument_id = instrument_id or self.instrument_base_url.rstrip("/").rsplit("/", 1)[-1]
        self.bearer_token = bearer_token
        self.timeout = timeout
        self._urlopen = urlopen_func or urlopen

    @staticmethod
    def _normalize_base_url(api_base_url: str, instrument_id: str | None) -> str:
        base = api_base_url.rstrip("/")
        if instrument_id is None:
            return base

        encoded_instrument_id = quote(instrument_id, safe="")
        return f"{base}/api/v1/instruments/{encoded_instrument_id}"

    @staticmethod
    def _api_origin(api_base_url: str) -> str:
        trimmed = api_base_url.strip()
        if "://" not in trimmed:
            trimmed = f"http://{trimmed}"
        parsed = urlparse(trimmed)
        if not parsed.netloc:
            raise ValueError(f"Could not parse API host from URL: {api_base_url!r}")
        scheme = parsed.scheme or "http"
        return f"{scheme}://{parsed.netloc}"

    @classmethod
    def _derive_common_api_base(cls, api_base_url: str) -> str:
        return f"{cls._api_origin(api_base_url).rstrip('/')}/api"

    def set_bearer_token(self, bearer_token: str | None) -> None:
        self.bearer_token = bearer_token

    def _wait_request_timeout(self, wait_seconds: int | None, server_default: int) -> float:
        """HTTP timeout long enough to cover a server-side wait plus margin."""
        budget = wait_seconds if wait_seconds is not None else server_default
        return max(self.timeout, float(budget) + WAIT_TIMEOUT_MARGIN_SECONDS)

    def _build_url(
        self,
        base_root: str,
        path: str,
        params: Mapping[str, JsonValue | Sequence[JsonValue] | None] | None = None,
    ) -> str:
        relative_path = path if path.startswith("/") else f"/{path}"
        url = f"{base_root}{relative_path}"
        if not params:
            return url

        items: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    items.append((key, coerce_query_value(item)))
                continue
            items.append((key, coerce_query_value(value)))

        if not items:
            return url

        return f"{url}?{urlencode(items, doseq=True)}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, JsonValue | Sequence[JsonValue] | None] | None = None,
        json_body: JsonMapping | None = None,
        route: Literal["instrument", "common"] = "instrument",
        send_bearer: bool = True,
        request_timeout: float | None = None,
    ) -> JsonValue | None:
        base_root = self.instrument_base_url if route == "instrument" else self.common_api_base_url
        url = self._build_url(base_root, path, params=params)
        payload: bytes | None = None
        headers = {"Accept": "application/json"}
        if send_bearer and self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if json_body is not None:
            payload = json.dumps(dict(json_body)).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, data=payload, headers=headers, method=method.upper())
        timeout = self.timeout if request_timeout is None else request_timeout

        try:
            with self._urlopen(request, timeout=timeout) as response:
                return decode_response(response.read(), getattr(response, "headers", None))
        except HTTPError as exc:
            error_payload = decode_response(exc.read(), exc.headers)
            fallback = exc.reason or self.api_error_fallback
            message, can_force = error_details(error_payload, fallback)
            raise TriosClientError(
                message=message,
                status_code=exc.code,
                url=url,
                payload=error_payload,
                can_force=can_force,
            ) from exc
        except URLError as exc:
            raise TriosClientError(
                message=str(exc.reason),
                url=url,
            ) from exc
        except OSError as exc:
            raise TriosClientError(
                message=str(exc),
                url=url,
            ) from exc
