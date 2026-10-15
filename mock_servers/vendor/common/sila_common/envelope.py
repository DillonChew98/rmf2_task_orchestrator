"""SiLA Request / Envelope builders and payload JSON codec.

Request.Payload / Envelope.Payload / Envelope.Details are UTF-8 JSON strings on
the wire (empty string means none). ``encode_any`` / ``decode_any`` remain for
legacy Structure-inside-Any helpers and documentation type XML.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, ClassVar, Mapping, NamedTuple
import re

from sila2.framework.data_types.any import SilaAnyType
from sila2.framework.utils import xml_node_to_normalized_string

from .codec import dumps_json, loads_json

ENVELOPE_VERSION = 2


class InvalidPayloadError(ValueError):
    """Raised when Request/Envelope Payload or Details is not valid JSON."""


class AnyPayload(ABC):
    """Typed structure for command payloads (named fields, PascalCase).

    Subclasses are frozen dataclasses whose field names match FDL
    ``DataTypeDefinition`` catalogs. On the wire they serialize as JSON objects
    via ``dumps_payload``. Fields listed in ``_ANY_FIELDS`` are open maps
    (nested JSON objects); ``encode_any`` / ``to_sila_any`` remain for tests.
    """

    _TYPE_XML: ClassVar[str]
    _ANY_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def type_xml(cls) -> str:
        return cls._TYPE_XML

    def to_native(self) -> Any:
        """PascalCase Structure native value for sila2 Any encoding.

        Returns a ``NamedTuple`` (not a dict). sila2's Structure encoder treats
        mappings incorrectly for List fields (it iterates the whole mapping).
        """
        if not is_dataclass(self):
            raise TypeError(f"{type(self).__name__} must be a dataclass")
        cls = type(self)
        any_fields = cls._ANY_FIELDS
        values: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in any_fields:
                values[f.name] = encode_any(value)
            else:
                values[f.name] = _encode_structure_field(value)
        native_cls = getattr(cls, "_NativeTuple", None)
        if native_cls is None:
            native_cls = NamedTuple(  # type: ignore[misc]
                f"{cls.__name__}Native",
                [(f.name, Any) for f in fields(self)],
            )
            cls._NativeTuple = native_cls  # type: ignore[attr-defined]
        return native_cls(**values)

    def as_dict(self) -> dict[str, Any]:
        """Plain Python dict suitable for JSON serialization."""
        if not is_dataclass(self):
            raise TypeError(f"{type(self).__name__} must be a dataclass")
        return {f.name: _jsonable_field(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> AnyPayload:
        """Build from a PascalCase (or snake_case) mapping; missing keys use defaults."""
        if not is_dataclass(cls):
            raise TypeError(f"{cls.__name__} must be a dataclass")
        raw = data or {}
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in raw:
                kwargs[f.name] = raw[f.name]
            else:
                snake = _snake(f.name)
                if snake in raw:
                    kwargs[f.name] = raw[snake]
        return cls(**kwargs)  # type: ignore[call-arg]

    def to_sila_any(self) -> SilaAnyType:
        return SilaAnyType(self.type_xml(), self.to_native())


def _jsonable_field(value: Any) -> Any:
    if isinstance(value, AnyPayload):
        return value.as_dict()
    if isinstance(value, list):
        return [_jsonable_field(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def dumps_payload(value: Any) -> str:
    """Serialize a payload/details value to a wire JSON string (empty string = none)."""
    if value is None:
        return ""
    if isinstance(value, AnyPayload) or (is_dataclass(value) and not isinstance(value, type)):
        value = _jsonable_field(value)
    return dumps_json(value, numpy=True)


def loads_payload(raw: Any) -> Any:
    """Decode a wire Payload/Details field to a Python value (``None`` = none).

    Accepts JSON strings, already-decoded values, or ``AnyPayload`` instances.
    Raises ``InvalidPayloadError`` on invalid JSON.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return loads_json(raw)
        except ValueError as exc:
            raise InvalidPayloadError(str(exc)) from exc
    if isinstance(raw, AnyPayload):
        return raw.as_dict()
    if isinstance(raw, (dict, list, bool, int, float)):
        return raw
    if hasattr(raw, "_asdict"):
        return dict(raw._asdict())
    raise InvalidPayloadError(f"Unsupported Payload/Details type: {type(raw).__name__}")


class Outcome:
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    REJECTED = "Rejected"
    RUNNING = "Running"


class Code:
    REQUEST_INVALID_PAYLOAD = "sila.request.invalid_payload"
    TRIOS_AUTOSAMPLER_MOTION_NOT_STARTED = "trios.autosampler.motion_not_started"
    OPUS_MEASUREMENT_FAILED = "opus.measurement.failed"
    OPUS_NOT_CONNECTED = "opus.not_connected"
    OPUS_API = "opus.api"
    OPUS_UNKNOWN_FUNCTION = "opus.unknown_function"
    GPC_STATE_INVALID = "gpc.state.invalid"
    GPC_AUTOMATION = "gpc.automation"
    NMR_INSTRUMENT = "nmr.instrument"
    NMR_VALIDATION = "nmr.validation"

    @staticmethod
    def trios_http(status_code: int | None) -> str:
        if status_code is None:
            return "trios.http"
        return f"trios.http.{status_code}"


def _dt(xml: str) -> str:
    return xml_node_to_normalized_string(xml)


_BASIC_XML = {
    "Boolean": _dt('<DataType xmlns="http://www.sila-standard.org"><Basic>Boolean</Basic></DataType>'),
    "Integer": _dt('<DataType xmlns="http://www.sila-standard.org"><Basic>Integer</Basic></DataType>'),
    "Real": _dt('<DataType xmlns="http://www.sila-standard.org"><Basic>Real</Basic></DataType>'),
    "String": _dt('<DataType xmlns="http://www.sila-standard.org"><Basic>String</Basic></DataType>'),
}
_LIST_OF_ANY_XML = _dt(
    '<DataType xmlns="http://www.sila-standard.org"><List><DataType><Basic>Any</Basic></DataType></List></DataType>'
)
_DICT_XML = _dt(
    """<DataType xmlns="http://www.sila-standard.org">
      <List>
        <DataType>
          <Structure>
            <Element>
              <Identifier>Key</Identifier>
              <DisplayName>Key</DisplayName>
              <Description>Map key</Description>
              <DataType><Basic>String</Basic></DataType>
            </Element>
            <Element>
              <Identifier>Value</Identifier>
              <DisplayName>Value</DisplayName>
              <Description>Map value</Description>
              <DataType><Basic>Any</Basic></DataType>
            </Element>
          </Structure>
        </DataType>
      </List>
    </DataType>"""
)
_VOID_XML = SilaAnyType.create_void().type_xml


def _encode_structure_field(value: Any) -> Any:
    """Encode a Structure field value for sila2 (nested AnyPayload → NamedTuple)."""
    if isinstance(value, AnyPayload):
        return value.to_native()
    if isinstance(value, list):
        return [_encode_structure_field(item) for item in value]
    return value


def encode_any(value: Any) -> SilaAnyType:
    """Encode a Python value as SiLA Any (legacy / documentation helpers)."""
    if isinstance(value, SilaAnyType):
        return value
    if isinstance(value, AnyPayload):
        return value.to_sila_any()
    if value is None:
        return SilaAnyType.create_void()
    if isinstance(value, bool):
        return SilaAnyType(_BASIC_XML["Boolean"], value)
    if isinstance(value, int):
        return SilaAnyType(_BASIC_XML["Integer"], value)
    if isinstance(value, float):
        return SilaAnyType(_BASIC_XML["Real"], value)
    if isinstance(value, str):
        return SilaAnyType(_BASIC_XML["String"], value)
    if isinstance(value, list):
        return SilaAnyType(_LIST_OF_ANY_XML, [encode_any(item) for item in value])
    if isinstance(value, dict):
        entries = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"dict keys must be str; got {type(key).__name__}")
            entries.append({"Key": key, "Value": encode_any(item)})
        return SilaAnyType(_DICT_XML, entries)
    raise TypeError(f"Unsupported Any value type: {type(value).__name__}")


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, Mapping):
        return entry[name]
    return getattr(entry, name)


def _to_plain(value: Any) -> Any:
    """Convert sila2 Structure NamedTuples / nested Anys to plain Python."""
    if isinstance(value, SilaAnyType):
        return decode_any(value)
    if isinstance(value, AnyPayload):
        return value.as_dict()
    if hasattr(value, "_asdict"):
        return {k: _to_plain(v) for k, v in value._asdict().items()}
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        return [_to_plain(v) for v in value]
    return value


def _is_key_value_map_type(type_xml: str) -> bool:
    return (
        "<List>" in type_xml
        and "<Structure>" in type_xml
        and "<Identifier>Key</Identifier>" in type_xml
        and "<Identifier>Value</Identifier>" in type_xml
    )


def _top_level_kind(type_xml: str) -> str | None:
    match = re.search(
        r"<DataType[^>]*>\s*<(Structure|List|Basic|Constrained)",
        type_xml,
    )
    return match.group(1) if match else None


def decode_any(value: Any) -> Any:
    """Decode a SiLA Any (or already-native Python value) to the envelope table."""
    if not isinstance(value, SilaAnyType):
        return value
    type_xml, native = value
    if native is None or type_xml == _VOID_XML:
        return None
    if _is_key_value_map_type(type_xml):
        return {_entry_field(entry, "Key"): decode_any(_entry_field(entry, "Value")) for entry in native}
    kind = _top_level_kind(type_xml)
    if kind == "Structure":
        return _to_plain(native)
    if kind == "List":
        return [
            decode_any(item) if isinstance(item, SilaAnyType) else _to_plain(item)
            for item in native
        ]
    if "<Basic>Boolean</Basic>" in type_xml:
        return bool(native)
    if "<Basic>Integer</Basic>" in type_xml:
        return int(native)
    if "<Basic>Real</Basic>" in type_xml:
        return float(native)
    if "<Basic>String</Basic>" in type_xml:
        return str(native)
    return native


def structure_type_xml(elements: list[tuple[str, str, str, str]]) -> str:
    """Build a normalized ``DataType`` XML for a Structure Any.

    Each element is ``(identifier, display_name, description, datatype_xml)``
    where ``datatype_xml`` is an inner ``<DataType>...</DataType>`` fragment
    (no xmlns), e.g. from ``sila_common.fdl.basic_dt``.
    """
    from .fdl import structure_dt

    return _dt(
        structure_dt(elements).replace(
            "<DataType>",
            '<DataType xmlns="http://www.sila-standard.org">',
            1,
        )
    )


def _snake(name: str) -> str:
    chars: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper() or (i + 1 < len(name) and name[i + 1].islower())):
            chars.append("_")
        chars.append(ch.lower())
    return "".join(chars)


def _field(obj: Any, name: str, default: Any = ...) -> Any:
    snake = _snake(name)
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
        if snake in obj:
            return obj[snake]
    if hasattr(obj, name):
        return getattr(obj, name)
    if hasattr(obj, snake):
        return getattr(obj, snake)
    if default is not ...:
        return default
    raise TypeError(f"Request/Envelope is missing field {name}")


@dataclass(frozen=True, slots=True)
class Request:
    version: int
    correlation_id: str
    timeout_seconds: int | None
    payload: Any


@dataclass(frozen=True, slots=True)
class Envelope:
    version: int = ENVELOPE_VERSION
    outcome: str = Outcome.SUCCEEDED
    code: str = ""
    message: str = ""
    correlation_id: str = ""
    payload: Any = None
    details: Any = None

    def to_sila(self) -> dict[str, Any]:
        if self.outcome == Outcome.RUNNING:
            code = ""
        elif self.outcome == Outcome.SUCCEEDED:
            code = self.code if self.code else "0"
        else:
            code = self.code
        message = self.message
        if self.outcome == Outcome.SUCCEEDED and not message:
            message = "success"
        return {
            "Version": self.version,
            "Outcome": self.outcome,
            "Code": code,
            "Message": message,
            "CorrelationId": self.correlation_id,
            "Payload": dumps_payload(self.payload),
            "Details": dumps_payload(self.details),
        }


def parse_request(raw: Any) -> Request:
    """Parse a sila2 Request structure. ``TimeoutSeconds == 0`` becomes ``None``.

    Raises ``InvalidPayloadError`` when Payload is not valid JSON.
    """
    if isinstance(raw, Request):
        timeout = raw.timeout_seconds
        return Request(
            version=raw.version,
            correlation_id=raw.correlation_id,
            timeout_seconds=None if timeout == 0 else timeout,
            payload=loads_payload(raw.payload),
        )
    timeout = int(_field(raw, "TimeoutSeconds"))
    return Request(
        version=int(_field(raw, "Version")),
        correlation_id=str(_field(raw, "CorrelationId", "")),
        timeout_seconds=None if timeout == 0 else timeout,
        payload=loads_payload(_field(raw, "Payload", "")),
    )


def try_parse_request(raw: Any) -> tuple[Request | None, dict[str, Any] | None]:
    """Parse a Request or return ``(None, rejected Envelope dict)`` on bad JSON."""
    try:
        return parse_request(raw), None
    except InvalidPayloadError as exc:
        try:
            correlation_id = str(_field(raw, "CorrelationId", ""))
        except Exception:
            correlation_id = ""
        return None, rejected(
            code=Code.REQUEST_INVALID_PAYLOAD,
            message=str(exc),
            correlation_id=correlation_id,
        )


def request(
    *,
    payload: Any = None,
    timeout_seconds: int | None = 0,
    correlation_id: str = "",
    version: int = ENVELOPE_VERSION,
) -> dict[str, Any]:
    """Build a SiLA Request structure dict for sila2 clients and tests."""
    timeout = 0 if timeout_seconds is None else int(timeout_seconds)
    return {
        "Version": version,
        "CorrelationId": correlation_id,
        "TimeoutSeconds": timeout,
        "Payload": dumps_payload(payload),
    }


def parse_envelope(raw: Any) -> Envelope:
    if isinstance(raw, Envelope):
        return raw
    return Envelope(
        version=int(_field(raw, "Version", ENVELOPE_VERSION)),
        outcome=str(_field(raw, "Outcome")),
        code=str(_field(raw, "Code", "")),
        message=str(_field(raw, "Message", "")),
        correlation_id=str(_field(raw, "CorrelationId", "")),
        payload=loads_payload(_field(raw, "Payload", "")),
        details=loads_payload(_field(raw, "Details", "")),
    )


def succeeded(
    *,
    payload: Any = None,
    message: str = "success",
    correlation_id: str = "",
    version: int = ENVELOPE_VERSION,
    code: str = "0",
) -> dict[str, Any]:
    return Envelope(
        version=version,
        outcome=Outcome.SUCCEEDED,
        code=code,
        message=message,
        correlation_id=correlation_id,
        payload=payload,
    ).to_sila()


def running(
    *,
    payload: Any = None,
    message: str = "",
    correlation_id: str = "",
    version: int = ENVELOPE_VERSION,
) -> dict[str, Any]:
    return Envelope(
        version=version,
        outcome=Outcome.RUNNING,
        message=message,
        correlation_id=correlation_id,
        payload=payload,
    ).to_sila()


def failed(
    *,
    code: str,
    message: str,
    payload: Any = None,
    details: Any = None,
    correlation_id: str = "",
    version: int = ENVELOPE_VERSION,
) -> dict[str, Any]:
    return Envelope(
        version=version,
        outcome=Outcome.FAILED,
        code=code,
        message=message,
        correlation_id=correlation_id,
        payload=payload,
        details=details,
    ).to_sila()


def rejected(
    *,
    code: str,
    message: str,
    payload: Any = None,
    details: Any = None,
    correlation_id: str = "",
    version: int = ENVELOPE_VERSION,
) -> dict[str, Any]:
    return Envelope(
        version=version,
        outcome=Outcome.REJECTED,
        code=code,
        message=message,
        correlation_id=correlation_id,
        payload=payload,
        details=details,
    ).to_sila()
