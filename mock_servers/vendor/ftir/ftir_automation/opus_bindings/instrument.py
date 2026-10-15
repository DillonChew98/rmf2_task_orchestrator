"""Curated bindings for OPUS instrument-control functions.

Optics, diagnosis, temperature/vacuum control, stage movement, aperture, and
accessory control. Pass OPUS 3-letter parameter codes as keyword arguments;
use ``ftir.opus.describe("MoveStage")`` to inspect available codes.

Note: most hardware functions take no ``file_id``/``block_name`` — only the
wrappers whose underlying OPUS functions accept them expose those arguments.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from ftir_automation.hts_xt import HtsXtController, HtsXtGeometry
from ftir_automation.opus_bindings.proxy import _CommandGroup

FileId = Optional[Union[int, list]]
BlockName = Optional[Union[str, list]]

# GetStagePosition must land within this distance of the commanded pose.
_STAGE_POSITION_TOLERANCE_UM = 1000


class InstrumentCommands(_CommandGroup):
    """Spectrometer hardware control (optics, stage, temperature, accessories)."""

    def optic_setup(self, **params: Any) -> list:
        """Run OPUS OpticSetup — configure the optical bench."""
        return self._fn.OpticSetup(**params)

    def diagnosis(self, **params: Any) -> list:
        """Run OPUS Diagnosis — instrument self-diagnosis."""
        return self._fn.Diagnosis(**params)

    def temperature_control(self, **params: Any) -> list:
        """Run OPUS TemperatureControl — sample temperature control."""
        return self._fn.TemperatureControl(**params)

    def vacuum_control(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS VacuumControl — evacuate/vent the sample compartment."""
        return self._fn.VacuumControl(file_id=file_id, block_name=block_name, **params)

    def send_command(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SendCommand — send a raw command to the instrument firmware."""
        return self._fn.SendCommand(file_id=file_id, block_name=block_name, **params)

    def stage_control(self, **params: Any) -> list:
        """Run OPUS Stage_Control — microscope/mapping stage control."""
        return self._fn.Stage_Control(**params)

    def move_stage(self, **params: Any) -> list:
        """Run OPUS MoveStage — move the stage to a position (NDV, NOX, NOY)."""
        return self._fn.MoveStage(**params)

    def open_drawer(self, **_params: Any) -> list:
        """Present the HTS-XT tray: center, then the unload pose.

        Coordinates and ``NDV`` come from ``hts_xt.json``. Raises if the stage
        readout is more than 1 mm from the unload pose.
        """
        geometry = HtsXtGeometry.load(auto_calibrate_a1=False)
        response = HtsXtController(geometry, self.move_stage).unload_plate()
        x_um, y_um = geometry.unload_plate_poses()[-1]
        self._confirm_stage_at(geometry.stage_device, x_um, y_um)
        return response

    def close_drawer(self, **_params: Any) -> list:
        """Return the HTS-XT tray to the plate center.

        Coordinates and ``NDV`` come from ``hts_xt.json``. Raises if the stage
        readout is more than 1 mm from the center pose.
        """
        geometry = HtsXtGeometry.load(auto_calibrate_a1=False)
        response = HtsXtController(geometry, self.move_stage).load_plate()
        x_um, y_um = geometry.load_plate_pose()
        self._confirm_stage_at(geometry.stage_device, x_um, y_um)
        return response

    def _confirm_stage_at(self, device: str, x_um: int, y_um: int) -> None:
        kwargs: dict[str, Any] = {"NDV": device} if device else {}
        x, y = _parse_stage_xy(self.get_stage_position(**kwargs))
        if (
            abs(x - x_um) > _STAGE_POSITION_TOLERANCE_UM
            or abs(y - y_um) > _STAGE_POSITION_TOLERANCE_UM
        ):
            raise RuntimeError(
                f"Stage at ({x:.0f}, {y:.0f}) um, expected ({x_um}, {y_um}) um "
                f"within {_STAGE_POSITION_TOLERANCE_UM} um"
            )

    def get_stage_position(self, **params: Any) -> list:
        """Run OPUS GetStagePosition — read the current stage coordinates (NDV)."""
        return self._fn.GetStagePosition(**params)

    def get_stage_name(self, **params: Any) -> list:
        """Run OPUS GetStageName — read the connected stage identifier (IDV)."""
        return self._fn.GetStageName(**params)

    def get_number_of_axes(self, **params: Any) -> list:
        """Run OPUS GetNumberOfAxes — number of stage axes available (NDV)."""
        return self._fn.GetNumberOfAxes(**params)

    def move_stage_to_reference_position(self, **params: Any) -> list:
        """Run OPUS MoveStageToReferencePosition — home the stage to its reference (NDV, RPI)."""
        return self._fn.MoveStageToReferencePosition(**params)

    def set_reference_position(self, **params: Any) -> list:
        """Run OPUS SetReferencePosition — define the stage reference position (NDV, RPI, RXC, RYC, RZC)."""
        return self._fn.SetReferencePosition(**params)

    def is_reference_plate_available(self, **params: Any) -> list:
        """Run OPUS IsReferencePlateAvailable — check the internal reference plate (NDV)."""
        return self._fn.IsReferencePlateAvailable(**params)

    def aperture_control(self, **params: Any) -> list:
        """Run OPUS ApertureControl — set the aperture."""
        return self._fn.ApertureControl(**params)

    def atr_pressure_control(self, **params: Any) -> list:
        """Run OPUS ATRPressureControl — control the ATR pressure applicator."""
        return self._fn.ATRPressureControl(**params)

    def do_align_mode(self, **params: Any) -> list:
        """Run OPUS DoAlignMode — run the interferometer alignment."""
        return self._fn.DoAlignMode(**params)

    def auto_sampler(self, **params: Any) -> list:
        """Run OPUS AutoSampler — autosampler control."""
        return self._fn.AutoSampler(**params)

    def twister_ctrl(self, **params: Any) -> list:
        """Run OPUS TwisterCtrl — Twister plate-handling robot control (TCP, TMC, TPS, TDP, ...)."""
        return self._fn.TwisterCtrl(**params)

    def sample_wheel(self, **params: Any) -> list:
        """Run OPUS SampleWheel — sample wheel positioning."""
        return self._fn.SampleWheel(**params)


def _parse_stage_xy(response: list) -> tuple[float, float]:
    """Read X/Y micrometres from an OPUS GetStagePosition response.

    The coordinate line is ``index, axis_count, x, y, z``.
    """
    for line in response or []:
        parts = [part.strip() for part in str(line).split(",")]
        if len(parts) < 5:
            continue
        try:
            return float(parts[2]), float(parts[3])
        except ValueError:
            continue
    raise RuntimeError(f"Could not read stage X/Y from {response!r}")
