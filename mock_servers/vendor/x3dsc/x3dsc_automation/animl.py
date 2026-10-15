"""AnIML (ASTM analytical data XML) writer for X3 DSC runs.

Produces AnIML core schema 0.90 documents with one Sample and one ExperimentStep
per enabled sensor. Each Result holds Time (independent), Temperature, and Heat
Flow series as base64 EncodedValueSets of little-endian IEEE-754 Float64.

Stdlib-only (no numpy) so it matches the rest of the x3dsc package.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


def _b64_f64(values: list[float]) -> str:
    """Encode floats as little-endian Float64 base64 (AnIML EncodedValueSet)."""
    if not values:
        return ""
    return base64.b64encode(struct.pack(f"<{len(values)}d", *values)).decode()


def _param(name: str, value: object) -> str:
    return (
        f'        <Parameter name={quoteattr(name)}>'
        f'<S>{escape(str(value))}</S></Parameter>'
    )


@dataclass(slots=True)
class DscSensorRun:
    """One sensor's acquired (or simulated) DSC curve for a single sample."""

    sensor: str  # "A", "B", or "C"
    sample_name: str
    sample_mass_mg: float
    pan_type: str
    reference_pan: str
    time_s: list[float]
    temperature_c: list[float]
    heat_flow_mw: list[float]

    def __post_init__(self) -> None:
        n = len(self.time_s)
        if not (len(self.temperature_c) == n and len(self.heat_flow_mw) == n):
            raise ValueError(
                f"sensor {self.sensor}: time/temperature/heat_flow length mismatch "
                f"({n}/{len(self.temperature_c)}/{len(self.heat_flow_mw)})"
            )


@dataclass(slots=True)
class DscMethod:
    """Procedure / acquisition parameters shared across sensors in a run."""

    ramp_rate_c_per_min: float = 10.0
    start_temp_c: float = 25.0
    end_temp_c: float = 300.0
    purge_gas: str = "N2"
    flow_rate_ml_per_min: float = 50.0


def build_dsc_animl(
    runs: list[DscSensorRun],
    method: DscMethod,
    instrument: str = "X3 DSC",
) -> str:
    """Build an AnIML 0.90 XML string for one or more sensor runs."""
    if not runs:
        raise ValueError("at least one DscSensorRun is required")

    sample_blocks: list[str] = []
    step_blocks: list[str] = []

    method_params = "\n".join(
        [
            _param("RampRate_C_per_min", method.ramp_rate_c_per_min),
            _param("StartTemp_C", method.start_temp_c),
            _param("EndTemp_C", method.end_temp_c),
            _param("PurgeGas", method.purge_gas),
            _param("FlowRate_mL_per_min", method.flow_rate_ml_per_min),
            _param("Instrument", instrument),
        ]
    )

    for i, run in enumerate(runs, start=1):
        sample_id = f"sensor{run.sensor}"
        n = len(run.time_s)

        sample_blocks.append(
            f"""    <Sample name={quoteattr(run.sample_name)} sampleID={quoteattr(sample_id)}>
      <Category name="Description">
{_param("Sensor", run.sensor)}
{_param("SampleMass_mg", run.sample_mass_mg)}
{_param("PanType", run.pan_type)}
{_param("ReferencePan", run.reference_pan)}
      </Category>
    </Sample>"""
        )

        step_blocks.append(
            f"""    <ExperimentStep name={quoteattr(f"DSC sensor {run.sensor}: {run.sample_name}")} experimentStepID={quoteattr(f"step{i}")}>
      <Technique name="Differential Scanning Calorimetry" uri="https://animl.org/techniques/dsc.atdd"/>
      <Infrastructure>
        <SampleReferenceSet>
          <SampleReference sampleID={quoteattr(sample_id)} role="sample" samplePurpose="consumed"/>
        </SampleReferenceSet>
      </Infrastructure>
      <Method name="Acquisition">
      <Category name="Procedure">
{method_params}
      </Category>
      <Category name="Sample">
{_param("Sensor", run.sensor)}
{_param("SampleMass_mg", run.sample_mass_mg)}
{_param("PanType", run.pan_type)}
{_param("ReferencePan", run.reference_pan)}
      </Category>
      </Method>
      <Result name="DSC curve">
        <SeriesSet name="Thermogram" length="{n}">
          <Series name="Time" seriesID="time" seriesType="Float64"
                  dependency="independent" plotScale="linear">
            <EncodedValueSet>{_b64_f64(run.time_s)}</EncodedValueSet>
            <Unit label="s" quantity="time"/>
          </Series>
          <Series name="Temperature" seriesID="temperature" seriesType="Float64"
                  dependency="dependent" plotScale="linear">
            <EncodedValueSet>{_b64_f64(run.temperature_c)}</EncodedValueSet>
            <Unit label="degC" quantity="temperature"/>
          </Series>
          <Series name="Heat Flow" seriesID="heat_flow" seriesType="Float64"
                  dependency="dependent" plotScale="linear">
            <EncodedValueSet>{_b64_f64(run.heat_flow_mw)}</EncodedValueSet>
            <Unit label="mW" quantity="heat flow"/>
          </Series>
        </SeriesSet>
      </Result>
    </ExperimentStep>"""
        )

    samples_xml = "\n".join(sample_blocks)
    steps_xml = "\n".join(step_blocks)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<AnIML xmlns="urn:org:astm:animl:schema:core:draft:0.90" version="0.90">
  <SampleSet>
{samples_xml}
  </SampleSet>
  <ExperimentStepSet>
{steps_xml}
  </ExperimentStepSet>
</AnIML>
"""


def write_dsc_animl(
    path: Path | str,
    runs: list[DscSensorRun],
    method: DscMethod,
    instrument: str = "X3 DSC",
) -> Path:
    """Write AnIML XML to ``path`` and return the resolved Path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_dsc_animl(runs, method, instrument=instrument), encoding="utf-8")
    return out
