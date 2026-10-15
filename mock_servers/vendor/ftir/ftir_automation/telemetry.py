"""FTIR metric instruments and declarative OPUS parameter → gauge map.

OpenTelemetry is optional: if ``opentelemetry`` is not installed the
helpers become no-ops so the SiLA server still runs without the ``otel``
extra.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

_log = logging.getLogger(__name__)
_spectrum_log = logging.getLogger("ftir.spectrum")

# OPUS block → (x_unit, y_unit). Matches ASM cube conventions.
_BLOCK_AXIS_UNITS: dict[str, tuple[str, str]] = {
    "AB": ("1/cm", "AU"),
    "TR": ("1/cm", "%"),
    "RFL": ("1/cm", "(unitless)"),
    "Refl": ("1/cm", "(unitless)"),
    "IgSm": ("cm", "(unitless)"),
    "IgRf": ("cm", "(unitless)"),
}

try:
    from opentelemetry import metrics, trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL = True
except ImportError:  # pragma: no cover
    metrics = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    _OTEL = False

try:
    from lab_observability import semconv as sc
except ImportError:  # pragma: no cover — names duplicated below as fallbacks

    class sc:  # type: ignore[no-redef]
        ATTR_SILA_ERROR_IDENTIFIER = "sila.error.identifier"
        ATTR_OPUS_FUNCTION = "opus.function"
        ATTR_OPUS_THREAD_ID = "opus.thread.id"
        ATTR_OPUS_PROGRESS_PERCENT = "opus.progress.percent"
        ATTR_OPUS_SESSION_ID = "opus.session.id"
        ATTR_OPUS_HOST = "opus.host"
        ATTR_OPUS_PORT = "opus.port"
        ATTR_FTIR_RESOLUTION = "ftir.resolution"
        ATTR_FTIR_SAMPLE_SCANS = "ftir.sample_scans"
        ATTR_FTIR_BACKGROUND_SCANS = "ftir.background_scans"
        ATTR_FTIR_APODIZATION = "ftir.apodization"
        ATTR_FTIR_ZERO_FILL = "ftir.zero_fill_factor"
        ATTR_FTIR_PHASE_CORRECTION = "ftir.phase_correction"
        ATTR_FTIR_EXPERIMENT_FILE = "ftir.experiment_file"
        ATTR_FTIR_SPECTRUM_BLOCK_COUNT = "ftir.spectrum.block_count"
        ATTR_FTIR_SPECTRUM_POINTS = "ftir.spectrum.points"
        ATTR_FTIR_WAVENUMBER_MIN = "ftir.wavenumber.min"
        ATTR_FTIR_WAVENUMBER_MAX = "ftir.wavenumber.max"
        ATTR_FTIR_OUTPUT_PATH = "ftir.output.path"
        ATTR_FTIR_ASM_PATH = "ftir.asm.path"
        ATTR_FTIR_EXPORT_BYTES = "ftir.export.bytes"
        ATTR_LAB_RUN_ID = "lab.run.id"
        ATTR_LAB_SAMPLE_ID = "lab.sample.id"
        ATTR_OUTCOME = "outcome"
        ATTR_KIND = "kind"
        ATTR_FORMAT = "format"
        ATTR_REASON = "reason"
        ATTR_ACTION = "action"
        ATTR_CHANNEL = "channel"
        ATTR_CODE = "code"
        OUTCOME_OK = "ok"
        OUTCOME_ERROR = "error"
        OUTCOME_OPUS_ERROR = "opus_error"
        OUTCOME_TIMEOUT = "timeout"
        KIND_SAMPLE = "sample"
        KIND_BACKGROUND = "background"
        FORMAT_ASM = "asm"
        REASON_CONNECTION_REFUSED = "connection_refused"
        REASON_IMPORT_ERROR = "import_error"
        REASON_OPUS_ERROR = "opus_error"
        ACTION_OPEN = "open"
        ACTION_CLOSE = "close"
        CHANNEL_SAMPLE = "sample"
        CHANNEL_REFERENCE = "reference"
        METRIC_OPUS_SESSION_DURATION = "opus.session.duration"
        METRIC_OPUS_SESSION_FAILURES = "opus.session.failures"
        METRIC_OPUS_FUNCTION_DURATION = "opus.function.duration"
        METRIC_OPUS_THREAD_TIMEOUTS = "opus.thread.timeouts"
        METRIC_FTIR_MEASUREMENT_DURATION = "ftir.measurement.duration"
        METRIC_FTIR_MEASUREMENT_COUNT = "ftir.measurement.count"
        METRIC_FTIR_SCANS_TOTAL = "ftir.scans.total"
        METRIC_FTIR_BACKGROUND_AGE = "ftir.background.age"
        METRIC_FTIR_DRAWER_CYCLES = "ftir.drawer.cycles.total"
        METRIC_FTIR_STAGE_MOVES = "ftir.stage.moves.total"
        METRIC_FTIR_EXPORT_DURATION = "ftir.export.duration"
        METRIC_FTIR_EXPORT_BYTES = "ftir.export.bytes"
        METRIC_FTIR_EXPORT_FAILURES = "ftir.export.failures"
        METRIC_FTIR_PKA = "ftir.interferogram.peak_amplitude"
        METRIC_FTIR_LWN = "ftir.laser.wavenumber"
        METRIC_FTIR_DETECTOR_GAIN = "ftir.detector.gain"
        METRIC_FTIR_SNR = "ftir.signal_to_noise"
        METRIC_FTIR_SCANNER_VELOCITY = "ftir.scanner.velocity"
        METRIC_FTIR_APERTURE = "ftir.aperture"
        METRIC_FTIR_TEMPERATURE = "ftir.instrument.temperature"
        METRIC_FTIR_QUALITY = "ftir.quality.test"
        METRIC_FTIR_INSTRUMENT_UP = "ftir.instrument.up"
        METRIC_FTIR_HEALTH_PROBE_SKIPPED = "ftir.health.probe.skipped"
        METRIC_FTIR_HEALTH_CODE_SKIPPED = "ftir.health.code.skipped"
        TRACER_FTIR = "lab.ftir"
        TRACER_OPUS = "lab.opus"
        METER_FTIR = "lab.ftir"


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().split()[0].replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_mm(value: Any) -> Optional[float]:
    """``'6 mm'`` / ``6`` → millimetres. ``'open'`` → None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"open", "none", ""}:
        return None
    return _to_float(value)


@dataclass(frozen=True)
class OpusParamSpec:
    """Maps an OPUS parameter code onto an OTel gauge.

    Missing codes are skipped silently (and counted once) so this ships
    without a live OPUS catalogue.
    """

    code: str
    metric: str
    unit: str
    coerce: Callable[[Any], Optional[float]] = _to_float
    extra_attrs: dict[str, str] | None = None


OPUS_PARAM_METRICS: tuple[OpusParamSpec, ...] = (
    OpusParamSpec("PKA", sc.METRIC_FTIR_PKA, "1"),
    OpusParamSpec("LWN", sc.METRIC_FTIR_LWN, "1/cm"),
    OpusParamSpec(
        "ASG",
        sc.METRIC_FTIR_DETECTOR_GAIN,
        "1",
        extra_attrs={sc.ATTR_CHANNEL: sc.CHANNEL_SAMPLE},
    ),
    OpusParamSpec(
        "ARG",
        sc.METRIC_FTIR_DETECTOR_GAIN,
        "1",
        extra_attrs={sc.ATTR_CHANNEL: sc.CHANNEL_REFERENCE},
    ),
    OpusParamSpec("SGN", sc.METRIC_FTIR_DETECTOR_GAIN, "1", extra_attrs={sc.ATTR_CHANNEL: sc.CHANNEL_SAMPLE}),
    OpusParamSpec("RGN", sc.METRIC_FTIR_DETECTOR_GAIN, "1", extra_attrs={sc.ATTR_CHANNEL: sc.CHANNEL_REFERENCE}),
    OpusParamSpec("SN1", sc.METRIC_FTIR_SNR, "1", extra_attrs={sc.ATTR_CODE: "SN1"}),
    OpusParamSpec("SN2", sc.METRIC_FTIR_SNR, "1", extra_attrs={sc.ATTR_CODE: "SN2"}),
    OpusParamSpec("SN3", sc.METRIC_FTIR_SNR, "1", extra_attrs={sc.ATTR_CODE: "SN3"}),
    OpusParamSpec("SN4", sc.METRIC_FTIR_SNR, "1", extra_attrs={sc.ATTR_CODE: "SN4"}),
    OpusParamSpec("VEL", sc.METRIC_FTIR_SCANNER_VELOCITY, "1"),
    OpusParamSpec("APT", sc.METRIC_FTIR_APERTURE, "mm", coerce=_parse_mm),
    OpusParamSpec("TMP", sc.METRIC_FTIR_TEMPERATURE, "Cel"),
    OpusParamSpec("QQD", sc.METRIC_FTIR_QUALITY, "1", extra_attrs={sc.ATTR_CODE: "QQD"}),
    OpusParamSpec("QQE", sc.METRIC_FTIR_QUALITY, "1", extra_attrs={sc.ATTR_CODE: "QQE"}),
    OpusParamSpec("QQF", sc.METRIC_FTIR_QUALITY, "1", extra_attrs={sc.ATTR_CODE: "QQF"}),
    OpusParamSpec("QQG", sc.METRIC_FTIR_QUALITY, "1", extra_attrs={sc.ATTR_CODE: "QQG"}),
)


def find_opus_code(parameters: dict, code: str) -> Any:
    """Search a nested OPUS parameter map (block → {code: value}) for *code*."""
    if not isinstance(parameters, dict):
        return None
    if code in parameters:
        value = parameters[code]
        if not isinstance(value, dict):
            return value
    for value in parameters.values():
        if isinstance(value, dict):
            found = find_opus_code(value, code)
            if found is not None:
                return found
    return None


class _NoOpSpan:
    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_span_context(self) -> Any:
        return None

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class _NoOpTracer:
    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpInstrument:
    def record(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None


class FtirTelemetry:
    """Lazy-created FTIR meters. Safe to construct without OpenTelemetry."""

    def __init__(self) -> None:
        self._last_background_mono: float | None = None
        self._skipped_codes: set[str] = set()
        self._up = 0.0
        if not _OTEL:
            self.tracer = _NoOpTracer()
            self._noop = True
            self.measurement_duration = _NoOpInstrument()
            self.measurement_count = _NoOpInstrument()
            self.scans_total = _NoOpInstrument()
            self.drawer_cycles = _NoOpInstrument()
            self.stage_moves = _NoOpInstrument()
            self.export_duration = _NoOpInstrument()
            self.export_bytes = _NoOpInstrument()
            self.export_failures = _NoOpInstrument()
            self.session_duration = _NoOpInstrument()
            self.session_failures = _NoOpInstrument()
            self.function_duration = _NoOpInstrument()
            self.thread_timeouts = _NoOpInstrument()
            self.probe_skipped = _NoOpInstrument()
            self.code_skipped = _NoOpInstrument()
            self._gauges: dict[str, Any] = {}
            return

        self._noop = False
        self.tracer = trace.get_tracer(sc.TRACER_FTIR)
        meter = metrics.get_meter(sc.METER_FTIR)
        self.measurement_duration = meter.create_histogram(
            sc.METRIC_FTIR_MEASUREMENT_DURATION, unit="s"
        )
        self.measurement_count = meter.create_counter(sc.METRIC_FTIR_MEASUREMENT_COUNT)
        self.scans_total = meter.create_counter(sc.METRIC_FTIR_SCANS_TOTAL)
        self.drawer_cycles = meter.create_counter(sc.METRIC_FTIR_DRAWER_CYCLES)
        self.stage_moves = meter.create_counter(sc.METRIC_FTIR_STAGE_MOVES)
        self.export_duration = meter.create_histogram(sc.METRIC_FTIR_EXPORT_DURATION, unit="s")
        self.export_bytes = meter.create_histogram(sc.METRIC_FTIR_EXPORT_BYTES, unit="By")
        self.export_failures = meter.create_counter(sc.METRIC_FTIR_EXPORT_FAILURES)
        self.session_duration = meter.create_histogram(sc.METRIC_OPUS_SESSION_DURATION, unit="s")
        self.session_failures = meter.create_counter(sc.METRIC_OPUS_SESSION_FAILURES)
        self.function_duration = meter.create_histogram(
            sc.METRIC_OPUS_FUNCTION_DURATION, unit="s"
        )
        self.thread_timeouts = meter.create_counter(sc.METRIC_OPUS_THREAD_TIMEOUTS)
        self.probe_skipped = meter.create_counter(sc.METRIC_FTIR_HEALTH_PROBE_SKIPPED)
        self.code_skipped = meter.create_counter(sc.METRIC_FTIR_HEALTH_CODE_SKIPPED)

        self._gauge_values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges = {}
        for spec in OPUS_PARAM_METRICS:
            if spec.metric not in self._gauges:
                self._gauges[spec.metric] = meter.create_gauge(spec.metric, unit=spec.unit)
        self._gauges[sc.METRIC_FTIR_INSTRUMENT_UP] = meter.create_gauge(
            sc.METRIC_FTIR_INSTRUMENT_UP, unit="1"
        )
        self._gauges[sc.METRIC_FTIR_BACKGROUND_AGE] = meter.create_gauge(
            sc.METRIC_FTIR_BACKGROUND_AGE, unit="s"
        )

    def start_span(self, name: str, **attrs: Any):
        span_cm = self.tracer.start_as_current_span(name)
        span = span_cm.__enter__()
        for key, value in attrs.items():
            if value is None or value == "":
                continue
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
            else:
                span.set_attribute(key, str(value))
        return span_cm, span

    def set_span_error(self, span: Any, message: str, *, outcome: str = "") -> None:
        if Status is None:
            return
        span.set_status(Status(StatusCode.ERROR, message))
        if outcome:
            span.set_attribute(sc.ATTR_OUTCOME, outcome)

    def record_measurement(
        self,
        *,
        kind: str,
        duration_s: float,
        outcome: str,
        scans: int = 0,
        resolution: float | None = None,
    ) -> None:
        attrs = {sc.ATTR_KIND: kind, sc.ATTR_OUTCOME: outcome}
        # resolution/scans are bounded for typical XPM presets; still omit from
        # histograms to keep cardinality low — they live on the span.
        self.measurement_duration.record(duration_s, {sc.ATTR_KIND: kind, sc.ATTR_OUTCOME: outcome})
        self.measurement_count.add(1, attrs)
        if outcome == sc.OUTCOME_OK and scans > 0:
            self.scans_total.add(scans, {sc.ATTR_KIND: kind})
        if kind == sc.KIND_BACKGROUND and outcome == sc.OUTCOME_OK:
            self._last_background_mono = time.monotonic()
        self._emit_background_age()

    def record_export(self, *, fmt: str, duration_s: float, size_bytes: int, ok: bool) -> None:
        attrs = {sc.ATTR_FORMAT: fmt}
        if ok:
            self.export_duration.record(duration_s, attrs)
            if size_bytes > 0:
                self.export_bytes.record(float(size_bytes), attrs)
        else:
            self.export_failures.add(1, attrs)

    def record_session(self, duration_s: float, *, ok: bool, reason: str = "") -> None:
        outcome = sc.OUTCOME_OK if ok else sc.OUTCOME_ERROR
        self.session_duration.record(duration_s, {sc.ATTR_OUTCOME: outcome})
        if not ok:
            self.session_failures.add(1, {sc.ATTR_REASON: reason or sc.REASON_OPUS_ERROR})

    def record_function(self, name: str, duration_s: float, outcome: str) -> None:
        self.function_duration.record(
            duration_s, {sc.ATTR_OPUS_FUNCTION: name, sc.ATTR_OUTCOME: outcome}
        )
        if outcome == sc.OUTCOME_TIMEOUT:
            self.thread_timeouts.add(1, {sc.ATTR_OPUS_FUNCTION: name})

    def record_drawer(self, action: str) -> None:
        self.drawer_cycles.add(1, {sc.ATTR_ACTION: action})

    def record_stage_move(self) -> None:
        self.stage_moves.add(1)

    def record_probe_skipped(self) -> None:
        self.probe_skipped.add(1)

    def set_instrument_up(self, up: bool) -> None:
        gauge = self._gauges.get(sc.METRIC_FTIR_INSTRUMENT_UP)
        if gauge is not None:
            gauge.set(1.0 if up else 0.0)
        self._up = 1.0 if up else 0.0
        self._emit_background_age()

    def _emit_background_age(self) -> None:
        gauge = self._gauges.get(sc.METRIC_FTIR_BACKGROUND_AGE)
        if gauge is None or self._last_background_mono is None:
            return
        gauge.set(time.monotonic() - self._last_background_mono)

    def record_opus_parameters(self, parameters: dict) -> None:
        """Emit health gauges for every code present; skip missing ones."""
        for spec in OPUS_PARAM_METRICS:
            raw = find_opus_code(parameters, spec.code)
            if raw is None:
                if spec.code not in self._skipped_codes:
                    self._skipped_codes.add(spec.code)
                    self.code_skipped.add(1, {sc.ATTR_CODE: spec.code})
                    _log.debug("OPUS health code %s not present; skipping", spec.code)
                continue
            value = spec.coerce(raw)
            if value is None:
                continue
            gauge = self._gauges.get(spec.metric)
            if gauge is None:
                continue
            gauge.set(value, spec.extra_attrs or {})


_TELEMETRY: FtirTelemetry | None = None


def mark_current_span_error(message: str, outcome: str = "opus_error") -> None:
    if not _OTEL:
        return
    span = trace.get_current_span()
    get_telemetry().set_span_error(span, message, outcome=outcome)
    span.set_attribute(sc.ATTR_OUTCOME, outcome)


def _spectrum_x_values(wavenumbers: Any) -> list[float]:
    try:
        return [float(v) for v in wavenumbers]
    except Exception:
        return []


def _spectrum_y_values(intensities: Any) -> list[float]:
    """Flatten the first spectral trace to a 1-D float list."""
    try:
        import numpy as np

        arr = np.asarray(intensities, dtype=float)
        if arr.ndim == 0:
            return [float(arr)]
        if arr.ndim == 1:
            return [float(v) for v in arr.tolist()]
        # (N, traces) — first channel
        return [float(v) for v in arr[:, 0].tolist()]
    except Exception:
        return []


def _spectrum_block_payload(
    *,
    run_id: str,
    sample_id: str | None,
    block: str,
    x_vals: list[float],
    y_vals: list[float],
) -> dict[str, Any]:
    x_unit, y_unit = _BLOCK_AXIS_UNITS.get(block, ("1/cm", "(unitless)"))
    return {
        "event": "ftir.spectrum.block",
        "run_id": run_id,
        "sample_id": sample_id or "",
        "block": block,
        "x_unit": x_unit,
        "y_unit": y_unit,
        "x": x_vals,
        "y": y_vals,
    }


def emit_spectrum_blocks(
    spectra: Sequence[Any],
    *,
    run_id: str,
    sample_id: str | None = None,
) -> int:
    """Emit one JSON log line per readable OPUS block for Loki / the dashboard.

    Arrays stay out of Prometheus. Each line is keyed by *run_id*. Returns the
    number of lines emitted.
    """
    if not run_id or not spectra:
        return 0
    emitted = 0
    for spectrum in spectra:
        block = str(getattr(spectrum, "block_name", "") or "")
        if not block:
            continue
        x_vals = _spectrum_x_values(getattr(spectrum, "wavenumbers", []))
        y_vals = _spectrum_y_values(getattr(spectrum, "intensities", None))
        if not x_vals or not y_vals or len(x_vals) != len(y_vals):
            _log.warning(
                "Skipping spectrum log for block %s: x=%d y=%d",
                block,
                len(x_vals),
                len(y_vals),
            )
            continue
        # One line, no pretty-print — Loki max_line_size raised to 8MB for Ig*.
        payload = _spectrum_block_payload(
            run_id=run_id,
            sample_id=sample_id,
            block=block,
            x_vals=x_vals,
            y_vals=y_vals,
        )
        _spectrum_log.info("%s", json.dumps(payload, separators=(",", ":")))
        emitted += 1
    return emitted


def emit_processed_spectrum(
    result: Any,
    *,
    run_id: str,
    sample_id: str | None = None,
) -> bool:
    """Emit one Loki line with raw transmittance and peak marks.

    *result* is a :class:`~ftir_automation.peaks.PeakPickResult` (or any object
    with ``block_name``, ``wavenumbers``, ``transmittance``, and ``peaks``).
    Falls back to ``corrected`` when transmittance is missing (older callers).
    """
    if not run_id or result is None:
        return False
    x_vals = _spectrum_x_values(getattr(result, "wavenumbers", []))
    y_src = getattr(result, "transmittance", None)
    if y_src is None:
        y_src = getattr(result, "corrected", [])
    y_vals = _spectrum_x_values(y_src)
    if not x_vals or not y_vals or len(x_vals) != len(y_vals):
        _log.warning(
            "Skipping processed spectrum log: x=%d y=%d",
            len(x_vals),
            len(y_vals),
        )
        return False
    peaks_out: list[dict[str, Any]] = []
    for peak in getattr(result, "peaks", None) or []:
        peaks_out.append(
            {
                "wavenumber": float(getattr(peak, "wavenumber", 0.0)),
                "height": float(getattr(peak, "height", 0.0)),
                "transmittance": float(getattr(peak, "transmittance", 0.0)),
                "intensity": str(getattr(peak, "intensity", "") or ""),
                "shape": str(getattr(peak, "shape", "") or ""),
            }
        )
    block = str(getattr(result, "block_name", "") or "")
    payload = {
        "event": "ftir.spectrum.processed",
        "run_id": run_id,
        "sample_id": sample_id or "",
        "block": block,
        "x_unit": "1/cm",
        "y_unit": "transmittance",
        "x": x_vals,
        "y": y_vals,
        "peaks": peaks_out,
    }
    _spectrum_log.info("%s", json.dumps(payload, separators=(",", ":")))
    return True


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _spectrum_summary(spectrum: Any, *, file_path: str) -> dict[str, Any] | None:
    block = str(getattr(spectrum, "block_name", "") or "")
    if not block:
        return None
    lo, hi = 0.0, 0.0
    try:
        wr = spectrum.wavenumber_range
        lo, hi = _as_float(wr[0]), _as_float(wr[1])
    except Exception:
        pass
    return {
        "BlockName": block,
        "NPoints": _as_int(getattr(spectrum, "n_points", 0)),
        "NTraces": _as_int(getattr(spectrum, "n_traces", 1), 1),
        "WavenumberMin": lo,
        "WavenumberMax": hi,
        "FilePath": str(getattr(spectrum, "file_path", "") or file_path or ""),
    }


def emit_measure_result(
    *,
    run_id: str,
    sample_id: str | None = None,
    operator: str = "",
    project: str = "",
    file_path: str = "",
    asm_path: str = "",
    spectra: Sequence[Any] | None = None,
    raw_response: Sequence[Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> bool:
    """Emit one JSON log line with the MeasureSample return fields.

    Full wavenumber and intensity arrays stay on ``ftir.spectrum.block`` lines.
    This summary is what the operator dashboard shows for a selected run.
    """
    if not run_id:
        return False
    summaries = []
    for spectrum in spectra or []:
        summary = _spectrum_summary(spectrum, file_path=file_path)
        if summary is not None:
            summaries.append(summary)
    payload = {
        "event": "ftir.measure.result",
        "run_id": run_id,
        "sample_id": sample_id or "",
        "Operator": operator or "",
        "Project": project or "",
        "FilePath": file_path or "",
        "AsmPath": asm_path or "",
        "Spectra": summaries,
        "RawResponse": [str(item) for item in (raw_response or [])],
        "Parameters": parameters if isinstance(parameters, dict) else {},
    }
    _spectrum_log.info("%s", json.dumps(payload, separators=(",", ":")))
    return True


def get_telemetry() -> FtirTelemetry:
    global _TELEMETRY
    if _TELEMETRY is None:
        _TELEMETRY = FtirTelemetry()
    return _TELEMETRY


def reset_telemetry_for_tests() -> None:
    global _TELEMETRY
    _TELEMETRY = None
