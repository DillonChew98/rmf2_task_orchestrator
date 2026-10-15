"""Canonical Request / Envelope FDL fragments and shared feature emitters."""

from __future__ import annotations

REQUEST_XML = """\
<DataTypeDefinition>
  <Identifier>Request</Identifier>
  <DisplayName>Request</DisplayName>
  <Description>Standard command request. Version is the envelope schema version. Empty CorrelationId means none. TimeoutSeconds 0 means the server default. Empty Payload means none.</Description>
  <DataType>
    <Structure>
      <Element>
        <Identifier>Version</Identifier>
        <DisplayName>Version</DisplayName>
        <Description>Envelope schema version. Currently 2.</Description>
        <DataType><Basic>Integer</Basic></DataType>
      </Element>
      <Element>
        <Identifier>CorrelationId</Identifier>
        <DisplayName>Correlation id</DisplayName>
        <Description>Caller correlation id. Empty means none. Echoed on the response.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
      <Element>
        <Identifier>TimeoutSeconds</Identifier>
        <DisplayName>Timeout (seconds)</DisplayName>
        <Description>Command timeout in seconds. 0 means the server default.</Description>
        <DataType><Basic>Integer</Basic></DataType>
      </Element>
      <Element>
        <Identifier>Payload</Identifier>
        <DisplayName>Payload</DisplayName>
        <Description>UTF-8 JSON string for the request body. Empty string means none. Prefer a JSON object matching the command DataTypeDefinition.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
    </Structure>
  </DataType>
</DataTypeDefinition>"""

ENVELOPE_XML = """\
<DataTypeDefinition>
  <Identifier>Envelope</Identifier>
  <DisplayName>Envelope</DisplayName>
  <Description>Standard command result. Outcome is Succeeded, Failed, Rejected, or Running. Code is 0 on Succeeded and empty on Running. Message defaults to success on Succeeded. Empty Details means none.</Description>
  <DataType>
    <Structure>
      <Element>
        <Identifier>Version</Identifier>
        <DisplayName>Version</DisplayName>
        <Description>Envelope schema version. Currently 2.</Description>
        <DataType><Basic>Integer</Basic></DataType>
      </Element>
      <Element>
        <Identifier>Outcome</Identifier>
        <DisplayName>Outcome</DisplayName>
        <Description>Succeeded, Failed, Rejected, or Running.</Description>
        <DataType>
          <Constrained>
            <DataType>
              <Basic>String</Basic>
            </DataType>
            <Constraints>
              <Set>
                <Value>Succeeded</Value>
                <Value>Failed</Value>
                <Value>Rejected</Value>
                <Value>Running</Value>
              </Set>
            </Constraints>
          </Constrained>
        </DataType>
      </Element>
      <Element>
        <Identifier>Code</Identifier>
        <DisplayName>Code</DisplayName>
        <Description>Additive lowercase dotted code on Failed/Rejected. 0 on Succeeded. Empty on Running.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
      <Element>
        <Identifier>Message</Identifier>
        <DisplayName>Message</DisplayName>
        <Description>Human-readable outcome message. Defaults to success on Succeeded.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
      <Element>
        <Identifier>CorrelationId</Identifier>
        <DisplayName>Correlation id</DisplayName>
        <Description>Echo of the request correlation id. Empty means none.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
      <Element>
        <Identifier>Payload</Identifier>
        <DisplayName>Payload</DisplayName>
        <Description>UTF-8 JSON string for the result body. Empty string means none. Prefer a JSON object matching the command DataTypeDefinition.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
      <Element>
        <Identifier>Details</Identifier>
        <DisplayName>Details</DisplayName>
        <Description>UTF-8 JSON string for diagnostics such as status_code, url, and canForce. Empty string means none.</Description>
        <DataType><Basic>String</Basic></DataType>
      </Element>
    </Structure>
  </DataType>
</DataTypeDefinition>"""

INTERNAL_ERROR_XML = """\
<DefinedExecutionError>
  <Identifier>InternalError</Identifier>
  <DisplayName>Internal error</DisplayName>
  <Description>The server could not build an envelope (connection drop, unexpected exception, or cancellation).</Description>
</DefinedExecutionError>"""

_REQUEST_PARAM = """\
<Parameter>
  <Identifier>Request</Identifier>
  <DisplayName>Request</DisplayName>
  <Description>Standard command request.</Description>
  <DataType><DataTypeIdentifier>Request</DataTypeIdentifier></DataType>
</Parameter>"""

_RESULT_RESPONSE = """\
<Response>
  <Identifier>Result</Identifier>
  <DisplayName>Result</DisplayName>
  <Description>Standard command envelope.</Description>
  <DataType><DataTypeIdentifier>Envelope</DataTypeIdentifier></DataType>
</Response>"""

_UPDATE_INTERMEDIATE = """\
<IntermediateResponse>
  <Identifier>Update</Identifier>
  <DisplayName>Update</DisplayName>
  <Description>Intermediate envelope with Outcome Running.</Description>
  <DataType><DataTypeIdentifier>Envelope</DataTypeIdentifier></DataType>
</IntermediateResponse>"""


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def basic_dt(name: str) -> str:
    return f"<DataType><Basic>{name}</Basic></DataType>"


def type_id(name: str) -> str:
    return f"<DataType><DataTypeIdentifier>{name}</DataTypeIdentifier></DataType>"


def list_dt(inner_datatype_xml: str) -> str:
    """FDL fragment: ``List`` of *inner_datatype_xml* (itself a ``DataType`` node)."""
    return f"<DataType><List>{inner_datatype_xml}</List></DataType>"


def structure_dt(elements: list[tuple[str, str, str, str]]) -> str:
    """FDL fragment: ``Structure`` ``DataType`` (no xmlns) for nesting or definitions.

    Each element is ``(identifier, display_name, description, datatype_xml)``.
    """
    parts: list[str] = ["<DataType><Structure>"]
    for identifier, display, description, datatype_xml in elements:
        parts.append("<Element>")
        parts.append(f"<Identifier>{identifier}</Identifier>")
        parts.append(f"<DisplayName>{esc(display)}</DisplayName>")
        parts.append(f"<Description>{esc(description)}</Description>")
        parts.append(datatype_xml)
        parts.append("</Element>")
    parts.append("</Structure></DataType>")
    return "".join(parts)


def xml_param(identifier: str, display: str, description: str, datatype_xml: str) -> str:
    return (
        "<Parameter>\n"
        f"  <Identifier>{identifier}</Identifier>\n"
        f"  <DisplayName>{esc(display)}</DisplayName>\n"
        f"  <Description>{esc(description)}</Description>\n"
        f"  {datatype_xml}\n"
        "</Parameter>"
    )


def xml_response(identifier: str, display: str, description: str, datatype_xml: str) -> str:
    return (
        "<Response>\n"
        f"  <Identifier>{identifier}</Identifier>\n"
        f"  <DisplayName>{esc(display)}</DisplayName>\n"
        f"  <Description>{esc(description)}</Description>\n"
        f"  {datatype_xml}\n"
        "</Response>"
    )


def xml_intermediate(identifier: str, display: str, description: str, datatype_xml: str) -> str:
    return (
        "<IntermediateResponse>\n"
        f"  <Identifier>{identifier}</Identifier>\n"
        f"  <DisplayName>{esc(display)}</DisplayName>\n"
        f"  <Description>{esc(description)}</Description>\n"
        f"  {datatype_xml}\n"
        "</IntermediateResponse>"
    )


def cmd(name: str, display: str, description: str, *, observable: bool = False) -> str:
    parts = [
        "<Command>",
        f"  <Identifier>{name}</Identifier>",
        f"  <DisplayName>{esc(display)}</DisplayName>",
        f"  <Description>{esc(description)}</Description>",
        f"  <Observable>{'Yes' if observable else 'No'}</Observable>",
        "  " + _REQUEST_PARAM.replace("\n", "\n  "),
        "  " + _RESULT_RESPONSE.replace("\n", "\n  "),
    ]
    if observable:
        parts.append("  " + _UPDATE_INTERMEDIATE.replace("\n", "\n  "))
    parts.append("  <DefinedExecutionErrors><Identifier>InternalError</Identifier></DefinedExecutionErrors>")
    parts.append("</Command>")
    return "\n".join(parts)


def prop(
    name: str,
    display: str,
    description: str,
    *,
    observable: bool = False,
    datatype_xml: str | None = None,
) -> str:
    return (
        "<Property>\n"
        f"  <Identifier>{name}</Identifier>\n"
        f"  <DisplayName>{esc(display)}</DisplayName>\n"
        f"  <Description>{esc(description)}</Description>\n"
        f"  <Observable>{'Yes' if observable else 'No'}</Observable>\n"
        f"  {datatype_xml or basic_dt('String')}\n"
        "</Property>"
    )


def feature_header(*, originator: str, category: str, version: str = "2.0") -> str:
    return (
        '<Feature xmlns="http://www.sila-standard.org" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'Category="{esc(category)}" FeatureVersion="{esc(version)}" '
        'MaturityLevel="Draft" '
        f'Originator="{esc(originator)}" SiLA2Version="1.0" '
        'xsi:schemaLocation="http://www.sila-standard.org '
        'https://gitlab.com/SiLA2/sila_base/raw/master/schema/FeatureDefinition.xsd">'
    )


def assemble(
    identifier: str,
    title: str,
    description: str,
    body: str,
    *,
    originator: str,
    category: str,
    version: str = "2.0",
    extra_errors: str = "",
) -> str:
    parts = [
        feature_header(originator=originator, category=category, version=version),
        f"<Identifier>{identifier}</Identifier>",
        f"<DisplayName>{esc(title)}</DisplayName>",
        f"<Description>{esc(description)}</Description>",
        REQUEST_XML,
        ENVELOPE_XML,
        body,
        INTERNAL_ERROR_XML,
    ]
    if extra_errors:
        parts.append(extra_errors)
    parts.append("</Feature>")
    return "\n".join(parts) + "\n"
