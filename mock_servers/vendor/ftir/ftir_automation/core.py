"""
Bruker FTIR automation core — wraps opuspy_api (external HTTP interpreter mode).

Prerequisites:
  - OPUS 9.2 must be running with the HTTP server enabled:
      "C:\\Program Files\\Bruker\\OPUS_9.2.4\\opus.exe" /HTTPSERVER=On /HTTPPORT=100
  - opuspy_api must be installed (see README.md)
  - numpy must be installed

Usage example::

    from ftir_automation import FtirAutomation, MeasurementConfig

    ftir = FtirAutomation(opus_ip="127.0.0.1", opus_port=100)
    ftir.connect()

    config = MeasurementConfig(
        resolution=4.0,
        sample_scans=32,
        background_scans=32,
        output_path=r"C:\\OPUS\\Data\\sample.0",
    )
    result = ftir.measure(config)
    for spectrum in result.spectra:
        print(spectrum.block_name, spectrum.wavenumber_range)
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Callable, Optional, TypeVar

import numpy as np

from ftir_automation.asm import enrich_asm_metadata, write_ftir_asm
from ftir_automation.envfile import ftir_output_dir
from ftir_automation.hts_xt import HtsXtController, HtsXtGeometry
from ftir_automation.models.instrument import (
    InstrumentSnapshot,
    MeasurementConfig,
    MeasurementResult,
    SpectrumData,
)
from ftir_automation.opus_bindings import (
    EvaluationCommands,
    FileCommands,
    InstrumentCommands,
    MeasurementCommands,
    OpusFunctionsProxy,
    ProcessingCommands,
)
from ftir_automation.opus_thread import _response_errors, run_threaded_function
from ftir_automation.telemetry import get_telemetry, mark_current_span_error, sc

log = logging.getLogger(__name__)

T = TypeVar("T")

# Default OPUS HTTP server connection parameters
DEFAULT_OPUS_IP = "127.0.0.1"
DEFAULT_OPUS_PORT = 100


# After UnloadAll, retry the read for a few hundred milliseconds while OPUS
# drops the exclusive handle.
_RELEASE_RETRY_S = 0.1
_RELEASE_ATTEMPTS = 5


def _opus_file_readable(path: str) -> bool:
    """True when *path* can be opened for reading."""
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _resolve_output_directory(config: MeasurementConfig) -> str:
    """Directory for the OPUS file and the derived ASM export.

    An explicit ``output_path`` directory wins. Otherwise ``output_directory``
    or ``FTIR_OUTPUT_DIR`` is applied and created. Empty means OPUS keeps the
    experiment-file folder.
    """
    if config.output_path:
        return os.path.dirname(config.output_path)
    directory = (config.output_directory or ftir_output_dir()).rstrip("\\/")
    if not directory:
        return ""
    os.makedirs(directory, exist_ok=True)
    config.output_directory = directory
    return directory


class FtirAutomation:
    """High-level automation interface for a Bruker FTIR spectrometer via OPUS.

    All communication goes through the OPUS HTTP server (external interpreter
    mode).  OPUS must be started with ``/HTTPSERVER=On /HTTPPORT=<port>``
    before calling :meth:`connect`.

    Args:
        opus_ip:   IP address of the machine running OPUS (default ``127.0.0.1``).
        opus_port: HTTP port OPUS is listening on (default ``100``).
        timeout:   Socket timeout in seconds for individual HTTP requests.
        logger:    Optional :class:`logging.Logger` forwarded to ``opuspy_api``.
    """

    def __init__(
        self,
        opus_ip: str = DEFAULT_OPUS_IP,
        opus_port: int = DEFAULT_OPUS_PORT,
        timeout: float = 30.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._opus_ip = opus_ip
        self._opus_port = opus_port
        self._timeout = timeout
        self._logger = logger

        # Set after connect()
        self._opus_api = None

        # OPUS command namespaces, instantiated in connect()
        self._measurement: Optional[MeasurementCommands] = None
        self._instrument: Optional[InstrumentCommands] = None
        self._processing: Optional[ProcessingCommands] = None
        self._files: Optional[FileCommands] = None
        self._evaluation: Optional[EvaluationCommands] = None
        self._opus_proxy: Optional[OpusFunctionsProxy] = None
        self._hts_geometry: Optional[HtsXtGeometry] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> InstrumentSnapshot:
        """Connect to the running OPUS process and verify the link.

        Returns:
            An :class:`InstrumentSnapshot` populated with version and host info.

        Raises:
            ConnectionError: If OPUS is unreachable or does not respond correctly.
            ImportError: If ``opuspy_api`` is not installed.
        """
        try:
            from opuspy_api import OpusAPI  # type: ignore[import]
        except ModuleNotFoundError as exc:
            raise ImportError(
                "opuspy_api is not installed. Run 'uv sync' in the ftir project "
                "(the wheel is referenced in pyproject.toml), or install it directly: "
                r"uv pip install C:\Users\Public\Documents\Bruker\OPUS_9.2.4"
                r"\OpusPythonAPI\dist\opuspy_api-20250701.1-py3-none-any.whl"
            ) from exc

        log.info(
            "Connecting to OPUS HTTP server at %s:%d …",
            self._opus_ip,
            self._opus_port,
        )
        tel = get_telemetry()
        span_cm, span = tel.start_span(
            "opus.connect",
            **{sc.ATTR_OPUS_HOST: self._opus_ip, sc.ATTR_OPUS_PORT: self._opus_port},
        )
        try:
            try:
                self._opus_api = OpusAPI(
                    opus_http_ip=self._opus_ip,
                    opus_http_port=self._opus_port,
                    logger=self._logger,
                )
            except Exception as exc:
                tel.set_span_error(span, str(exc), outcome=sc.REASON_CONNECTION_REFUSED)
                raise ConnectionError(
                    f"Failed to create OPUS API client ({self._opus_ip}:{self._opus_port}): {exc}"
                ) from exc

            self._measurement = MeasurementCommands(self._opus_api)
            self._instrument = InstrumentCommands(self._opus_api)
            self._processing = ProcessingCommands(self._opus_api)
            self._files = FileCommands(self._opus_api)
            self._evaluation = EvaluationCommands(self._opus_api)
            self._opus_proxy = OpusFunctionsProxy(self._opus_api)

            snapshot = self._safe("initial snapshot", self.get_snapshot, InstrumentSnapshot())
            if not snapshot.connected:
                tel.set_span_error(span, "version query failed", outcome=sc.REASON_CONNECTION_REFUSED)
                raise ConnectionError(
                    f"OPUS at {self._opus_ip}:{self._opus_port} did not respond to version query. "
                    "Ensure OPUS is running with /HTTPSERVER=On."
                )
            if snapshot.opus_version:
                span.set_attribute("opus.version", snapshot.opus_version)
            log.info("Connected — OPUS %s at %s", snapshot.opus_version, snapshot.host_ip)
            tel.set_instrument_up(True)
            return snapshot
        finally:
            span_cm.__exit__(None, None, None)

    def disconnect(self) -> None:
        """Release the API reference (does not stop OPUS)."""
        self._opus_api = None
        self._measurement = None
        self._instrument = None
        self._processing = None
        self._files = None
        self._evaluation = None
        self._opus_proxy = None
        log.info("Disconnected from OPUS")

    # ------------------------------------------------------------------
    # OPUS command namespaces
    # ------------------------------------------------------------------

    @property
    def measurement(self) -> MeasurementCommands:
        """Measurement and reference acquisition commands (OPUS Measure family)."""
        self._ensure_connected()
        return self._measurement

    @property
    def instrument(self) -> InstrumentCommands:
        """Instrument hardware control (optics, stage, temperature, accessories)."""
        self._ensure_connected()
        return self._instrument

    @property
    def processing(self) -> ProcessingCommands:
        """Spectral post-processing (baseline, smoothing, transforms, arithmetic)."""
        self._ensure_connected()
        return self._processing

    @property
    def files(self) -> FileCommands:
        """OPUS file lifecycle, export, plotting, and data block management."""
        self._ensure_connected()
        return self._files

    @property
    def evaluation(self) -> EvaluationCommands:
        """Peak picking, quality tests, library search, and chemometrics."""
        self._ensure_connected()
        return self._evaluation

    @property
    def opus(self) -> OpusFunctionsProxy:
        """Dynamic passthrough to all 401 OPUS functions by their original names.

        Includes discovery helpers: ``list_functions()`` and ``describe(name)``.
        """
        self._ensure_connected()
        return self._opus_proxy

    def hts_xt(
        self,
        *,
        config_path: str = "",
        auto_calibrate_a1: bool = True,
    ) -> HtsXtController:
        """Return an HTS-XT controller bound to this OPUS session."""
        self._ensure_connected()
        if self._hts_geometry is None or config_path:
            self._hts_geometry = HtsXtGeometry.load(
                config_path or None, auto_calibrate_a1=auto_calibrate_a1
            )
        return HtsXtController(self._hts_geometry, self.instrument.move_stage)

    # ------------------------------------------------------------------
    # Instrument state
    # ------------------------------------------------------------------

    def get_snapshot(self) -> InstrumentSnapshot:
        """Read current instrument information from OPUS.

        Returns:
            :class:`InstrumentSnapshot` with version, host IP, and language.
        """
        self._ensure_connected()
        snapshot = InstrumentSnapshot()
        try:
            snapshot.opus_version = self._opus_api.config.get_opus_version()
            snapshot.opus_version_ext = self._opus_api.config.get_opus_version_extended()
            snapshot.host_ip = self._opus_api.config.get_host_ip_address()
            snapshot.language = self._opus_api.config.get_language()
            snapshot.connected = True
        except Exception:
            log.error("Failed to read instrument snapshot", exc_info=True)
        return snapshot

    def get_opus_version(self) -> str:
        """Return the OPUS version string (e.g. ``'9.2'``)."""
        self._ensure_connected()
        return self._opus_api.config.get_opus_version()

    # ------------------------------------------------------------------
    # Background / reference measurement
    # ------------------------------------------------------------------

    def measure_background(
        self,
        config: Optional[MeasurementConfig] = None,
        *,
        save_reference: bool = True,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> bool:
        """Acquire a background (reference) spectrum.

        This calls the OPUS ``MeasureReference`` function to collect the
        background interferogram and optionally ``SaveReference`` to persist it.

        Args:
            config:         Measurement parameters (resolution, scans, etc.).
                            Defaults to a :class:`MeasurementConfig` with standard values.
            save_reference: If ``True``, also call ``SaveReference`` after collection.
            on_progress:    Optional callback ``(percent, status_text)`` invoked when
                            OPUS thread progress changes.

        Returns:
            ``True`` if the background was acquired successfully.
        """
        self._ensure_connected()
        config = config or MeasurementConfig()
        if not config.sample_name and config.experiment_file:
            config.sample_name = "Reference"
        params = config.to_opus_params()
        # Background measurement does not produce an output NAM; keep PTH when
        # an experiment file set it to the XPM directory (vendor HTS workflow).
        params.pop("NAM", None)
        if not config.experiment_file:
            params.pop("PTH", None)

        res = config.resolution if config.resolution is not None else 0.0
        nsr = config.background_scans if config.background_scans is not None else 0
        log.info(
            "Measuring background — RES=%s, NSR=%s, EXP=%s …",
            res,
            nsr,
            config.experiment_file or "(none)",
        )
        tel = get_telemetry()
        started = time.perf_counter()
        span_cm, span = tel.start_span(
            "ftir.measure_background",
            **{
                sc.ATTR_FTIR_RESOLUTION: res,
                sc.ATTR_FTIR_BACKGROUND_SCANS: nsr,
            },
        )
        outcome = sc.OUTCOME_OK
        try:
            completed, results = self._run_threaded_function(
                "MeasureReference", params, on_progress=on_progress
            )
            errors = _response_errors(results)
            if not completed or errors:
                outcome = sc.OUTCOME_TIMEOUT if not completed else sc.OUTCOME_OPUS_ERROR
                log.error("Background measurement failed: %s", errors or results)
                mark_current_span_error(str(errors or results), outcome=sc.OUTCOME_OPUS_ERROR)
                return False
            log.info("Background measurement complete")
        finally:
            tel.record_measurement(
                kind=sc.KIND_BACKGROUND,
                duration_s=time.perf_counter() - started,
                outcome=outcome,
                scans=nsr,
                resolution=res,
            )
            span_cm.__exit__(None, None, None)

        if save_reference:
            try:
                self._opus_api.opus_functions.SaveReference(file_id=None)
                log.info("Reference saved")
            except Exception:
                log.warning("SaveReference failed (background still acquired)", exc_info=True)
        return True

    # ------------------------------------------------------------------
    # Sample measurement
    # ------------------------------------------------------------------

    def measure(
        self,
        config: MeasurementConfig,
        *,
        background: bool = False,
        read_back: bool = True,
        export_asm: bool = True,
        asm_path: str = "",
        sample_name: str = "",
        operator: str = "",
        project: str = "",
        asm_metadata: Optional[dict] = None,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> MeasurementResult:
        """Perform a single FTIR measurement and optionally read back spectral data.

        The measurement uses the OPUS ``MeasureSample`` function (run in a
        background thread so progress can be polled) against the stored
        reference spectrum.  Acquire the reference first via
        :meth:`measure_background` or pass ``background=True``.

        Note: the monolithic OPUS ``Measure`` function is not used because it
        rejects external-interpreter calls on some benches (e.g. INVENIO-S,
        "No measurement possible, check parameters!" even with valid
        parameters), while the ``MeasureReference`` + ``MeasureSample``
        workflow works reliably.

        After the measurement completes, the resulting OPUS file is read back
        using :meth:`read_spectrum` unless *read_back* is ``False``.  When
        *export_asm* is ``True`` (default) and a saved OPUS path exists, an
        Allotrope FTIR ASM JSON document is written beside that OPUS file
        (or to *asm_path*).

        Args:
            config:       Measurement configuration (resolution, scans, output path…).
            background:   If ``True``, acquire a new background before the sample scan.
            read_back:    If ``True`` (default), load the file and return spectral data.
            export_asm:   If ``True`` (default), write an ASM JSON export after success.
            asm_path:     Destination ASM path; empty derives ``{opus_stem}.asm.json``.
            sample_name:  Optional sample name embedded in the ASM document / SNM.
            operator:     Optional experiment operator (ASM analyst when not overridden).
            project:      Optional experiment project stored with the result / ASM.
            asm_metadata: Optional ASM document-level field overrides for export.
            on_progress:  Optional callback ``(percent, status_text)`` invoked when
                          OPUS thread progress changes.

        Returns:
            :class:`MeasurementResult` with success flag, OPUS ``file_path``,
            ``asm_path``, and spectra when requested.
            ``success`` is false when OPUS fails or a configured measurement-record
            sink fails after the OPUS file is saved.
        """
        self._ensure_connected()
        if sample_name and not config.sample_name:
            config.sample_name = sample_name
        elif config.sample_name and not sample_name:
            sample_name = config.sample_name
        tel = get_telemetry()
        run_id = uuid.uuid4().hex
        started = time.perf_counter()
        res = config.resolution if config.resolution is not None else 0.0
        nss = config.sample_scans if config.sample_scans is not None else 0
        nsr = config.background_scans if config.background_scans is not None else 0
        span_cm, span = tel.start_span(
            "ftir.measure",
            **{
                sc.ATTR_LAB_RUN_ID: run_id,
                sc.ATTR_LAB_SAMPLE_ID: sample_name,
                sc.ATTR_FTIR_RESOLUTION: res,
                sc.ATTR_FTIR_SAMPLE_SCANS: nss,
                sc.ATTR_FTIR_BACKGROUND_SCANS: nsr,
                sc.ATTR_FTIR_EXPERIMENT_FILE: config.experiment_file or "",
                sc.ATTR_FTIR_OUTPUT_PATH: config.output_path or "",
            },
        )
        outcome = sc.OUTCOME_OK
        try:
            if background:
                if not self.measure_background(config, on_progress=on_progress):
                    outcome = sc.OUTCOME_OPUS_ERROR
                    tel.set_span_error(span, "background measurement failed", outcome=outcome)
                    return MeasurementResult(
                        success=False, raw_response=["background measurement failed"]
                    )

            output_dir = _resolve_output_directory(config)
            params = config.to_opus_params()
            log.info(
                "Starting measurement — RES=%s, NSS=%s, NAM=%s, PTH=%s, SNM=%s",
                res,
                nss,
                config.output_path or "(no path)",
                output_dir or "(experiment folder)",
                config.sample_name or "(none)",
            )

            completed, results = self._run_threaded_function(
                "MeasureSample", params, on_progress=on_progress
            )
            if not completed:
                outcome = sc.OUTCOME_TIMEOUT if results == ["timeout"] else sc.OUTCOME_ERROR
                tel.set_span_error(span, str(results), outcome=outcome)
                return MeasurementResult(success=False, raw_response=results)

            errors = _response_errors(results)
            if errors:
                log.error("Measurement reported errors: %s", errors)
                outcome = sc.OUTCOME_OPUS_ERROR
                mark_current_span_error(str(errors), outcome=outcome)
                return MeasurementResult(success=False, raw_response=results)

            saved_path = config.output_path
            if results:
                match = re.match(r'"(.+?)"', str(results[0]))
                if match:
                    saved_path = match.group(1)

            result = MeasurementResult(
                success=True,
                file_path=saved_path,
                raw_response=results,
            )
            if saved_path:
                span.set_attribute(sc.ATTR_FTIR_OUTPUT_PATH, saved_path)

            if read_back and saved_path:
                result.spectra = self._safe(
                    "read_spectrum",
                    lambda: self.read_spectrum(saved_path),
                    [],
                )
                if result.spectra:
                    span.set_attribute(sc.ATTR_FTIR_SPECTRUM_BLOCK_COUNT, len(result.spectra))
                    first = result.spectra[0]
                    span.set_attribute(sc.ATTR_FTIR_SPECTRUM_POINTS, first.n_points)
                    lo, hi = first.wavenumber_range
                    span.set_attribute(sc.ATTR_FTIR_WAVENUMBER_MIN, lo)
                    span.set_attribute(sc.ATTR_FTIR_WAVENUMBER_MAX, hi)

            if export_asm and saved_path:
                parameters = self._safe(
                    "read_parameters", lambda: self.read_parameters(saved_path), {}
                )
                if parameters:
                    tel.record_opus_parameters(parameters)
                snapshot = self._safe("get_snapshot", self.get_snapshot, InstrumentSnapshot())
                spectra = result.spectra or None

                written_asm = self._safe(
                    "export_asm",
                    lambda: self.export_asm(
                        saved_path,
                        output_path=asm_path,
                        sample_name=sample_name,
                        operator=operator,
                        project=project,
                        spectra=spectra,
                        parameters=parameters,
                        snapshot=snapshot,
                        asm_metadata=asm_metadata,
                        parameters_out=result.parameters,
                    ),
                    "",
                )
                result.asm_path = written_asm or ""
                if result.asm_path:
                    span.set_attribute(sc.ATTR_FTIR_ASM_PATH, result.asm_path)

            if saved_path:
                self._release_measured_file(saved_path)

            from ftir_automation.measurement_record import persist
            from ftir_automation.peaks import pick_peaks_from_spectra

            picked = pick_peaks_from_spectra(result.spectra) if result.spectra else None
            persist(
                result,
                picked,
                run_id=run_id,
                sample_id=sample_name,
                operator=operator,
                project=project,
                output_dir=output_dir,
            )
            if not result.success:
                outcome = sc.OUTCOME_ERROR
                detail = (
                    result.raw_response[-1]
                    if result.raw_response
                    else "measurement record failed"
                )
                tel.set_span_error(span, str(detail), outcome=outcome)
            return result
        finally:
            tel.record_measurement(
                kind=sc.KIND_SAMPLE,
                duration_s=time.perf_counter() - started,
                outcome=outcome,
                scans=nss,
                resolution=res,
            )
            span_cm.__exit__(None, None, None)

    def _run_threaded_function(
        self,
        fn_name: str,
        params: dict,
        *,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> tuple[bool, list]:
        """Launch an OPUS function in a background thread and poll until done.

        Args:
            fn_name: OPUS function name (e.g. ``MeasureSample``).
            params: OPUS parameter dict.
            on_progress: Optional callback ``(percent, status_text)`` when progress changes.

        Returns:
            ``(completed, result_lines)`` — *completed* is ``False`` on launch
            failure, polling error, or timeout.
        """
        return run_threaded_function(self._opus_api, fn_name, params, on_progress=on_progress)

    # ------------------------------------------------------------------
    # Data reading
    # ------------------------------------------------------------------

    def read_spectrum(self, file_path: str) -> list[SpectrumData]:
        """Load an OPUS file and extract all readable spectral data blocks.

        Args:
            file_path: Absolute path to the OPUS data file (e.g. ``sample.0``).

        Returns:
            List of :class:`SpectrumData`, one per readable block.

        Raises:
            FileNotFoundError: Propagated from OPUS if the file does not exist.
        """
        self._ensure_connected()
        log.info("Reading spectrum from: %s", file_path)
        tel = get_telemetry()
        span_cm, span = tel.start_span(
            "ftir.read_spectrum", **{sc.ATTR_FTIR_OUTPUT_PATH: file_path}
        )
        try:
            return self._read_spectrum_inner(file_path, span)
        finally:
            span_cm.__exit__(None, None, None)

    def _read_spectrum_inner(self, file_path: str, span) -> list[SpectrumData]:
        file_id, normalized_path = self._opus_api.file_loader.load_file(file_path)
        log.debug("Loaded file — id=%d, path=%s", file_id, normalized_path)

        try:
            reader = self._opus_api.BlockDataReader(opus_file_id=file_id)
            readable_blocks = reader.get_readable_data_blocks()
            log.debug("Readable blocks: %s", readable_blocks)

            spectra: list[SpectrumData] = []
            for block_name in readable_blocks:
                try:
                    block_data = reader.read_data_from_block(block_name)

                    wavenumbers = block_data.row_vector
                    intensities = block_data.data_array

                    # Ensure intensities is always 2-D (n_points, n_traces)
                    if intensities.ndim == 1:
                        intensities = intensities[:, np.newaxis]

                    spectra.append(
                        SpectrumData(
                            block_name=block_name,
                            wavenumbers=wavenumbers,
                            intensities=intensities,
                            parameters=block_data.block_parameters,
                            file_path=normalized_path,
                        )
                    )
                    log.info(
                        "Block '%s': %d pts, range %.1f–%.1f cm⁻¹",
                        block_name,
                        wavenumbers.shape[0],
                        float(wavenumbers.min()) if wavenumbers.size else 0,
                        float(wavenumbers.max()) if wavenumbers.size else 0,
                    )
                except Exception:
                    log.warning("Failed to read block '%s'", block_name, exc_info=True)
        finally:
            try:
                self._opus_api.file_loader.unload_file(file_id)
                log.debug("Unloaded file id=%d", file_id)
            except Exception:
                log.warning("Could not unload file id=%d", file_id, exc_info=True)

        span.set_attribute(sc.ATTR_FTIR_SPECTRUM_BLOCK_COUNT, len(spectra))
        return spectra

    def read_parameters(self, file_path: str) -> dict[str, dict]:
        """Read all OPUS parameter blocks from a file.

        Args:
            file_path: Absolute path to the OPUS data file.

        Returns:
            Dict mapping block name → parameter dict.
        """
        self._ensure_connected()
        tel = get_telemetry()
        span_cm, _span = tel.start_span(
            "ftir.read_parameters", **{sc.ATTR_FTIR_OUTPUT_PATH: file_path}
        )
        try:
            file_id, _ = self._opus_api.file_loader.load_file(file_path)
            try:
                reader = self._opus_api.BlockParameterReader(opus_file_id=file_id)
                return reader.read_all_parameters()
            finally:
                self._opus_api.file_loader.unload_file(file_id)
        finally:
            span_cm.__exit__(None, None, None)

    def export_asm(
        self,
        file_path: str,
        output_path: str = "",
        sample_name: str = "",
        *,
        operator: str = "",
        project: str = "",
        spectra: Optional[list[SpectrumData]] = None,
        parameters: Optional[dict] = None,
        snapshot: Optional[InstrumentSnapshot] = None,
        asm_metadata: Optional[dict] = None,
        parameters_out: Optional[dict] = None,
    ) -> str:
        """Export an OPUS data file to Allotrope FTIR ASM JSON.

        Args:
            file_path:    Absolute path to the OPUS data file.
            output_path:  Destination ``.asm.json`` path; empty derives from
                          *file_path* by replacing the extension with ``.asm.json``.
            sample_name:  Optional sample name for the ASM sample identifier.
            operator:     Optional experiment operator (ASM analyst when not overridden).
            project:      Optional experiment project written into the ASM document.
            spectra:      Optional pre-loaded spectra (avoids a second OPUS read
                          when called from :meth:`measure`).
            parameters:   Optional pre-loaded OPUS parameter map.
            snapshot:     Optional pre-loaded instrument snapshot.
            asm_metadata: Optional ASM document-level field overrides.
            parameters_out: Optional dict filled with the operator parameter
                          summary (same shape as ``summarize_operator_parameters``).

        Returns:
            Absolute path of the written ASM JSON file.
        """
        spectra, parameters, snapshot = self._load_export_context(
            file_path, spectra, parameters, snapshot, label="ASM"
        )

        tel = get_telemetry()
        started = time.perf_counter()
        span_cm, span = tel.start_span("ftir.export.asm")
        ok = False
        dest = output_path
        try:
            if not dest:
                root, _ext = os.path.splitext(file_path)
                dest = root + ".asm.json"

            _written, summary = write_ftir_asm(
                dest,
                spectra=spectra,
                snapshot=snapshot,
                sample_name=sample_name,
                parameters=parameters,
                asm_metadata=enrich_asm_metadata(
                    asm_metadata, operator=operator, project=project
                ),
            )
            if parameters_out is not None:
                parameters_out.clear()
                parameters_out.update(summary)
            size = os.path.getsize(dest) if os.path.isfile(dest) else 0
            span.set_attribute(sc.ATTR_FTIR_ASM_PATH, dest)
            span.set_attribute(sc.ATTR_FTIR_EXPORT_BYTES, size)
            log.info("Wrote ASM export: %s (%d blocks)", dest, len(spectra))
            ok = True
            return dest
        finally:
            size = os.path.getsize(dest) if dest and os.path.isfile(dest) else 0
            tel.record_export(
                fmt=sc.FORMAT_ASM,
                duration_s=time.perf_counter() - started,
                size_bytes=size,
                ok=ok,
            )
            span_cm.__exit__(None, None, None)

    # ------------------------------------------------------------------
    # GUI control
    # ------------------------------------------------------------------

    def hide_opus(self) -> bool:
        """Hide the OPUS application window."""
        self._ensure_connected()
        result = self._opus_api.gui.hide_opus()
        log.info("OPUS window hidden: %s", result)
        return result

    def show_opus(self) -> bool:
        """Restore the OPUS application window."""
        self._ensure_connected()
        result = self._opus_api.gui.show_opus()
        log.info("OPUS window shown: %s", result)
        return result

    # ------------------------------------------------------------------
    # Experiment / XPM helpers
    # ------------------------------------------------------------------

    def load_experiment(self, xpm_path: str) -> dict[str, str]:
        """Parse an OPUS experiment file (.XPM) and return its parameters.

        Args:
            xpm_path: Absolute path to the ``.XPM`` experiment template file.

        Returns:
            Dict of parameter shortcut → value strings.
        """
        self._ensure_connected()
        params = self._opus_api.config.read_experiment_parameters(xpm_path)
        log.info("Loaded experiment file: %s (%d params)", xpm_path, len(params))
        return params

    def config_from_experiment(self, xpm_path: str, **overrides) -> MeasurementConfig:
        """Build a :class:`MeasurementConfig` from an XPM experiment file.

        Reads the XPM, maps key parameters (RES, NSS, NSR) to the config
        dataclass, and applies any keyword overrides on top.

        Args:
            xpm_path:  Path to the ``.XPM`` file.
            **overrides: Keyword arguments matching :class:`MeasurementConfig` fields.

        Returns:
            A :class:`MeasurementConfig` ready to pass to :meth:`measure`.
        """
        raw = self.load_experiment(xpm_path)
        config = MeasurementConfig(experiment_file=xpm_path)

        _float_keys = {"RES": "resolution"}
        _int_keys = {
            "NSS": "sample_scans",
            "NSR": "background_scans",
        }
        _str_keys = {
            "AQM": "acquisition_mode",
            "ZFF": "zero_fill_factor",
            "APF": "apodization",
            "PHZ": "phase_correction",
        }
        for opus_key, attr in _float_keys.items():
            if opus_key in raw:
                try:
                    setattr(config, attr, float(raw[opus_key]))
                except ValueError:
                    pass
        for opus_key, attr in _int_keys.items():
            if opus_key in raw:
                try:
                    setattr(config, attr, int(raw[opus_key]))
                except ValueError:
                    pass
        for opus_key, attr in _str_keys.items():
            if opus_key in raw:
                setattr(config, attr, str(raw[opus_key]))

        for attr, value in overrides.items():
            if hasattr(config, attr):
                setattr(config, attr, value)

        return config

    # ------------------------------------------------------------------
    # Low-level / raw access
    # ------------------------------------------------------------------

    @property
    def api(self):
        """Direct access to the underlying ``opuspy_api.OpusAPI`` instance.

        Use for calling any of the 401 ``opus_functions`` not yet wrapped here.
        """
        self._ensure_connected()
        return self._opus_api

    def send_raw_command(self, command: str) -> tuple[bool, list]:
        """Send a raw OPUS C/S interpreter command.

        Args:
            command: Raw command string (e.g. ``"GET_VERSION"``).

        Returns:
            ``(success, response_lines)`` tuple.
        """
        self._ensure_connected()
        return self._opus_api.opus_request.send_command(command)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "FtirAutomation":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._opus_api is None:
            raise RuntimeError(
                "Not connected. Call connect() or use as a context manager first."
            )

    def _release_measured_file(self, path: str) -> None:
        """Unload the OPUS browser copy so a later copy can read *path*.

        ``MeasureSample`` keeps the spectrum open. Readback unloads only the
        copy it loaded, so the original file can stay locked. ``UnloadAll``
        drops browser files and leaves the stored reference in place.
        """
        if not path or not os.path.isfile(path) or _opus_file_readable(path):
            return
        try:
            self.files.unload_all()
        except Exception:
            log.warning("UnloadAll failed for %s", path, exc_info=True)
        for attempt in range(_RELEASE_ATTEMPTS):
            if _opus_file_readable(path):
                log.info("Released OPUS lock on %s", path)
                return
            if attempt + 1 < _RELEASE_ATTEMPTS:
                time.sleep(_RELEASE_RETRY_S)
        log.warning("OPUS still holds %s after UnloadAll", path)

    def _safe(self, label: str, fn, default: T) -> T:
        """Call *fn* and return its result; log the error and return *default* on failure."""
        try:
            return fn()
        except Exception:
            log.error("Failed to read %s", label, exc_info=True)
            return default

    def _load_export_context(
        self,
        file_path: str,
        spectra: Optional[list[SpectrumData]],
        parameters: Optional[dict],
        snapshot: Optional[InstrumentSnapshot],
        *,
        label: str,
    ) -> tuple[list[SpectrumData], dict, InstrumentSnapshot]:
        """Shared preamble for :meth:`export_asm`.

        Validates *file_path* and lazily fills in spectra/parameters/snapshot
        that the caller did not already have on hand (e.g. from :meth:`measure`).
        """
        self._ensure_connected()
        if not file_path:
            raise ValueError(f"file_path is required for {label} export")

        if spectra is None:
            spectra = self.read_spectrum(file_path)
        if parameters is None:
            parameters = self._safe(
                "read_parameters", lambda: self.read_parameters(file_path), {}
            )
        if snapshot is None:
            snapshot = self._safe("get_snapshot", self.get_snapshot, InstrumentSnapshot())
        return spectra, parameters, snapshot


def get_ftir(
    opus_ip: str = DEFAULT_OPUS_IP,
    opus_port: int = DEFAULT_OPUS_PORT,
) -> FtirAutomation:
    """Convenience factory — create an :class:`FtirAutomation` and connect.

    Args:
        opus_ip:   IP address of the OPUS host.
        opus_port: OPUS HTTP server port.

    Returns:
        A connected :class:`FtirAutomation` instance.
    """
    ftir = FtirAutomation(opus_ip=opus_ip, opus_port=opus_port)
    ftir.connect()
    return ftir


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ftir = get_ftir()
    snap = ftir.get_snapshot()
    print(f"OPUS version : {snap.opus_version}")
    print(f"Host IP      : {snap.host_ip}")
    print(f"Language     : {snap.language}")
