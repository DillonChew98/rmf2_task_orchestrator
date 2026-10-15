"""A stand-in for FtirAutomation, the object FtirExecutor owns.

Implements every member ftir/sila/ftir_impl.py and FtirExecutor touch, extracted
from those files rather than guessed.

Three of them are easy to miss:

- `_opus_api`. FtirExecutor.connected reads `self.ftir._opus_api is not None`, so
  a fake without that attribute breaks the connection property outright.
- `instrument`. StageControlService calls getattr(ftir.instrument, method_name)
  for seven named stage methods, each returning a list of strings.
- `hts_xt()`. The plate-stage commands, and MeasureSample with a Well, move
  through it. It returns the REAL HtsXtController on the real well geometry,
  driving the fake stage above, so the micrometre maths is upstream's own.

The measurement writes a genuine ASM document through the real
ftir_automation.asm.write_ftir_asm. That is worth the trouble: MeasurementService
reads the file back in _record_asm_context and populates seven of its properties
from it, so those go from empty to real values with nothing faked in the SiLA
layer.
"""

from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from ..harness.run_engine import TimedRun
from .spectra import spectrum_parameters, synthetic_absorbance

BACKGROUND_FRACTION = 0.3
# The real run_threaded_function polls OPUS once a second and reports each change.
PROGRESS_INTERVAL_S = 1.0

SERIAL_NUMBER = "MOCK-FTIR-0001"
DETECTOR_TYPE = "DTGS"
BEAMSPLITTER_TYPE = "KBr"
ANALYST = "mock"


class FakeStageCommands:
    """What StageControlService reaches through `ftir.instrument`.

    Every real InstrumentCommands method returns a list of response strings.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.drawer_open = False
        self.position = (0.0, 0.0)
        self.reference_position = (0.0, 0.0)

    def open_drawer(self, **_kw) -> list[str]:
        with self._lock:
            self.drawer_open = True
        return ["OK"]

    def close_drawer(self, **_kw) -> list[str]:
        with self._lock:
            self.drawer_open = False
        return ["OK"]

    def move_stage(self, **kwargs) -> list[str]:
        # StageControlService sends OPUS parameter codes, not friendly names:
        # ftir_impl.py:769 calls _run_stage("move_stage", NDV=Device, NOX=X, NOY=Y).
        with self._lock:
            x = float(kwargs.get("NOX", self.position[0]))
            y = float(kwargs.get("NOY", self.position[1]))
            self.position = (x, y)
        return ["OK", f"X={x}", f"Y={y}"]

    def move_stage_to_reference_position(self, **_kw) -> list[str]:
        with self._lock:
            self.position = self.reference_position
        return ["OK"]

    def set_reference_position(self, **_kw) -> list[str]:
        with self._lock:
            self.reference_position = self.position
        return ["OK"]

    def get_stage_position(self, **_kw) -> list[str]:
        with self._lock:
            return [f"X={self.position[0]}", f"Y={self.position[1]}"]

    def get_stage_name(self, **_kw) -> list[str]:
        return ["Mock XY stage"]

    def get_number_of_axes(self, **_kw) -> list[str]:
        return ["2"]

    def is_reference_plate_available(self, **_kw) -> list[str]:
        return ["1"]


class FakeFtirAutomation:
    """The simulated OPUS-backed spectrometer."""

    def __init__(self, run_seconds: float, output_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.run = TimedRun(run_seconds)
        self.background_run = TimedRun(max(1.0, run_seconds * BACKGROUND_FRACTION))

        # FtirExecutor.connected reads this attribute directly.
        self._opus_api: Any = None
        self._opus_ip = "127.0.0.1"
        self._opus_port = 100

        self.instrument = FakeStageCommands()
        self.opus: Any = None  # set on connect; the real OpusFunctionsProxy
        self.has_background = False
        self._hts_geometry: Any = None

        self._tmpdir = tempfile.TemporaryDirectory(prefix="mock-ftir-")
        self.output_dir = Path(output_dir) if output_dir else Path(self._tmpdir.name)

    # -- connection -------------------------------------------------------

    def connect(self):
        from ftir_automation.opus_bindings import OpusFunctionsProxy

        from .opuspy_stub import OpusAPI

        with self._lock:
            if self._opus_api is None:
                self._opus_api = OpusAPI()
                self.opus = OpusFunctionsProxy(self._opus_api)
        return self.get_snapshot()

    def disconnect(self) -> None:
        with self._lock:
            self._opus_api = None
            self.opus = None

    def get_snapshot(self):
        from ftir_automation.models import InstrumentSnapshot

        return InstrumentSnapshot(
            connected=self._opus_api is not None,
            opus_version="9.2",
            opus_version_ext="9.2.4 (mock)",
            host_ip=self._opus_ip,
            language="English",
        )

    def show_opus(self) -> bool:
        return True

    def hide_opus(self) -> bool:
        return True

    def hts_xt(self, *, config_path: str = "", auto_calibrate_a1: bool = True):
        """The real controller on the real geometry, moving the fake stage.

        A1 calibration is always off: on a first use it reads OPUS's HTS-XT.ini and
        writes the result back into marvel_digitalisation's hts_xt.json.
        """
        from ftir_automation.hts_xt import HtsXtController, HtsXtGeometry

        self._require_connected()
        with self._lock:
            if self._hts_geometry is None or config_path:
                self._hts_geometry = HtsXtGeometry.load(config_path or None, auto_calibrate_a1=False)
            return HtsXtController(self._hts_geometry, self.instrument.move_stage)

    # -- measurement: the timed part --------------------------------------

    def measure_background(self, config=None, *, save_reference: bool = True, on_progress=None) -> bool:
        self._require_connected()
        self._run_reporting(self.background_run, on_progress, "Measuring background")
        with self._lock:
            self.has_background = True
        return True

    def measure(
        self,
        config,
        *,
        background: bool = False,
        read_back: bool = True,
        export_asm: bool = True,
        asm_path: str = "",
        sample_name: str = "",
        operator: str = "",
        project: str = "",
        asm_metadata: dict | None = None,
        on_progress=None,
    ):
        """Take the configured time, reporting progress, then produce a real result on disk.

        MeasureSample is observable: each on_progress call becomes one Update. There
        is no abort -- the FTIR feature set has no cancel command, and inventing one
        would make the mock lie.
        """
        from ftir_automation.models import MeasurementResult, SpectrumData

        self._require_connected()
        if background:
            self.measure_background(config, on_progress=on_progress)

        self._run_reporting(self.run, on_progress, "Measuring sample")

        measurement_id = str(uuid.uuid4())
        name = sample_name or "mock-sample"
        stem = f"{name}-{measurement_id[:8]}"
        file_path = str(self.output_dir / f"{stem}.0")

        resolution = float(getattr(config, "resolution", 4.0) or 4.0)
        points = 1024
        wavenumbers, intensities = synthetic_absorbance(points)
        spectrum = SpectrumData(
            block_name="AB",
            wavenumbers=wavenumbers,
            intensities=intensities,
            parameters=spectrum_parameters(points, 400.0, 4000.0, resolution),
            file_path=file_path,
        )

        written_asm = ""
        if export_asm:
            written_asm = self.export_asm(
                file_path,
                asm_path or str(self.output_dir / f"{stem}.asm.json"),
                name,
                operator=operator,
                project=project,
                asm_metadata=asm_metadata,
                spectra=[spectrum],
                measurement_id=measurement_id,
            )

        return MeasurementResult(
            success=True,
            file_path=file_path,
            file_id=1,
            spectra=[spectrum],
            asm_path=written_asm,
            raw_response=["OK"],
        )

    # -- read-back and export ---------------------------------------------

    def read_spectrum(self, file_path: str, **_kwargs):
        from ftir_automation.models import SpectrumData

        self._require_connected()
        points = 1024
        wavenumbers, intensities = synthetic_absorbance(points)
        return [
            SpectrumData(
                block_name="AB",
                wavenumbers=wavenumbers,
                intensities=intensities,
                parameters=spectrum_parameters(points, 400.0, 4000.0, 4.0),
                file_path=file_path,
            )
        ]

    def read_parameters(self, file_path: str, **_kwargs) -> dict:
        self._require_connected()
        return spectrum_parameters(1024, 400.0, 4000.0, 4.0)

    def export_asm(
        self,
        file_path: str,
        output_path: str = "",
        sample_name: str = "",
        *,
        operator: str = "",
        project: str = "",
        spectra=None,
        parameters: dict | None = None,
        snapshot=None,
        asm_metadata: dict | None = None,
        parameters_out: dict | None = None,
        measurement_id: str | None = None,
    ) -> str:
        """Writes a genuine ASM document through the real writer, and returns its path.

        MeasurementService reads this back and fills seven of its properties from
        it, so the values below are what the dashboard ends up showing. Operator
        becomes the analyst through the real enrich_asm_metadata, unless
        asm_metadata names one; with neither, the analyst is "mock".
        """
        from ftir_automation.asm import enrich_asm_metadata, write_ftir_asm

        spectra = spectra or self.read_spectrum(file_path)
        metadata = enrich_asm_metadata(
            {
                "detector type": DETECTOR_TYPE,
                "beamsplitter type": BEAMSPLITTER_TYPE,
                "equipment serial number": SERIAL_NUMBER,
                **(asm_metadata or {}),
            },
            operator=operator,
            project=project,
        )
        metadata.setdefault("analyst", ANALYST)
        dest = output_path or str(Path(file_path).with_suffix(".asm.json"))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        written, summary = write_ftir_asm(
            dest,
            spectra,
            snapshot or self.get_snapshot(),
            sample_name=sample_name or "mock-sample",
            parameters=parameters or spectrum_parameters(1024, 400.0, 4000.0, 4.0),
            measurement_id=measurement_id or str(uuid.uuid4()),
            asm_metadata=metadata,
        )
        if parameters_out is not None:
            parameters_out.clear()
            parameters_out.update(summary)
        return dest

    def send_raw_command(self, command: str) -> tuple[bool, list[str]]:
        self._require_connected()
        return True, ["OK", f"echo: {command}"]

    # -- internals --------------------------------------------------------

    @staticmethod
    def _run_reporting(run: TimedRun, on_progress, status: str) -> None:
        """Run to the end, calling on_progress(percent, status) whenever the percent moves."""
        run.start(force=True)
        last = -1
        while True:
            done = run.wait_until_idle(PROGRESS_INTERVAL_S)
            percent = 100 if done else int(run.snapshot().progress * 100)
            if on_progress is not None and percent != last:
                on_progress(percent, status)
                last = percent
            if done:
                return

    def _require_connected(self) -> None:
        if self._opus_api is None:
            raise RuntimeError("Failed to open an OPUS session: not connected")

    def close(self) -> None:
        self._tmpdir.cleanup()
