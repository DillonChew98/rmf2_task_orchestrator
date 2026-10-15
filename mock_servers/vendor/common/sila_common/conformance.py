"""Conformance checks for envelope-standard SiLA feature XML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .fdl import ENVELOPE_XML, REQUEST_XML

SILA_NS = "http://www.sila-standard.org"
NS = {"sila": SILA_NS}
FORBIDDEN_IDENTIFIERS = frozenset({"ResultJson", "StatusJson", "ProgressJson", "PayloadJson"})


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    rule: str
    message: str


def repo_root(start: Path | None = None) -> Path:
    starts = [start] if start is not None else []
    starts.extend((Path(__file__).resolve(), Path.cwd().resolve()))
    for origin in starts:
        for candidate in (origin, *origin.parents):
            if not (candidate / "sila_common").is_dir():
                continue
            if any((candidate / name / "sila" / "features").is_dir() for name in ("sdt", "x3dsc", "ftir", "gpc", "nmr")):
                return candidate
    return Path.cwd().resolve()


def feature_xml_paths(root: Path | None = None) -> list[Path]:
    base = repo_root() if root is None else root
    return sorted(base.glob("*/sila/features/*.sila.xml"))


def _text(el: ET.Element | None, path: str = "sila:Identifier") -> str:
    if el is None:
        return ""
    return (el.findtext(path, default="", namespaces=NS) or "").strip()


def _type_id(el: ET.Element | None) -> str:
    return _text(el, "sila:DataType/sila:DataTypeIdentifier")


def feature_version_major(path: Path, xml: str | None = None) -> int | None:
    text = xml if xml is not None else path.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    raw = root.attrib.get("FeatureVersion", "")
    head = raw.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def check_feature(path: Path, xml: str | None = None) -> list[Violation]:
    text = xml if xml is not None else path.read_text(encoding="utf-8")
    violations: list[Violation] = []

    def add(rule: str, message: str) -> None:
        violations.append(Violation(path, rule, message))

    if REQUEST_XML not in text or ENVELOPE_XML not in text:
        add("canonical_types", "Request / Envelope XML must byte-match the sila_common.fdl canonical text")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        add("parse", f"Invalid XML: {exc}")
        return violations

    major = feature_version_major(path, text)
    if major != 2:
        add("feature_version", f"FeatureVersion major must be 2 (got {root.attrib.get('FeatureVersion', '')!r})")

    for ident in root.iter(f"{{{SILA_NS}}}Identifier"):
        name = (ident.text or "").strip()
        if name in FORBIDDEN_IDENTIFIERS:
            add("forbidden_identifiers", f"Forbidden identifier {name}")

    errors = root.findall("sila:DefinedExecutionError", NS)
    internal = [el for el in errors if _text(el) == "InternalError"]
    if len(internal) != 1:
        add("internal_error", f"Feature must declare exactly one InternalError (found {len(internal)})")

    for command in root.findall("sila:Command", NS):
        name = _text(command) or "<unknown>"
        parameters = command.findall("sila:Parameter", NS)
        if not parameters or _text(parameters[0]) != "Request" or _type_id(parameters[0]) != "Request":
            add("request_parameter", f"{name}: first parameter must be Request: Request")
        elif len(parameters) != 1:
            add("request_parameter", f"{name}: command must have exactly one parameter (Request)")

        responses = command.findall("sila:Response", NS)
        if len(responses) != 1 or _text(responses[0]) != "Result" or _type_id(responses[0]) != "Envelope":
            add("result_envelope", f"{name}: must have exactly one response Result: Envelope")

        intermediates = command.findall("sila:IntermediateResponse", NS)
        observable = _text(command, "sila:Observable") == "Yes"
        if observable:
            if (
                len(intermediates) != 1
                or _text(intermediates[0]) != "Update"
                or _type_id(intermediates[0]) != "Envelope"
            ):
                add("update_envelope", f"{name}: observable command must have exactly one Update: Envelope")
        elif intermediates:
            add("update_envelope", f"{name}: unobservable command must not declare intermediates")

    return violations


def scan_features(root: Path | None = None) -> list[tuple[Path, list[Violation]]]:
    return [(path, check_feature(path)) for path in feature_xml_paths(root)]
