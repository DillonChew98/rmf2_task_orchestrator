"""HTS-XT well geometry and plate-transport helpers.

Replicates the vendor AS-001 controller behaviour inside this repository.
Coordinates are micrometres (µm) and map to OPUS ``NOX`` / ``NOY``.
"""

from __future__ import annotations

import configparser
import glob
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_ROW_LABELS = "ABCDEFGHIJKLMNOP"
_PLATE_DIMENSIONS = {
    96: (8, 12),
    24: (4, 6),
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "hts_xt.json"
DEFAULT_STAGE_DEVICE = "TangoPCIe_HTSXT"


@dataclass(frozen=True)
class WellPosition:
    """Absolute stage coordinates for one well."""

    well: str
    row: int
    col: int
    x_um: int
    y_um: int


class HtsXtGeometry:
    """Well-to-micron geometry and load/unload poses for the HTS-XT stage."""

    def __init__(self, config: dict[str, Any]) -> None:
        plate_format = int(config.get("plate_format", 96))
        spacing_um_by_plate = config.get(
            "well_spacing_um_by_plate", {"96": 9000, "24": 18000}
        )
        if str(plate_format) not in spacing_um_by_plate:
            raise ValueError(
                f"No well spacing configured for plate format '{plate_format}'. "
                f"Configured: {list(spacing_um_by_plate.keys())}"
            )

        self.config = dict(config)
        self.plate_format: int = plate_format
        self.well_spacing_um: int = int(spacing_um_by_plate[str(plate_format)])
        self.a1_x_um: int = int(config.get("a1_x_um", 92160))
        self.a1_y_um: int = int(config.get("a1_y_um", 21600))
        self.center_x_um: int = int(config["center_x_um"])
        self.center_y_um: int = int(config["center_y_um"])
        self.unload_x_um: int = int(config["unload_x_um"])
        self.unload_y_um: int = int(config["unload_y_um"])
        self.stage_device: str = str(
            config.get("stage_device", DEFAULT_STAGE_DEVICE) or DEFAULT_STAGE_DEVICE
        )
        self.opus_methods_path: str = str(config.get("opus_methods_path", "") or "")
        self.default_transmission_xpm_path: str = str(
            config.get("default_transmission_xpm_path", "") or ""
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        auto_calibrate_a1: bool = True,
    ) -> HtsXtGeometry:
        """Load geometry from JSON, optionally calibrating A1 from HTS-XT.ini."""
        config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        if auto_calibrate_a1:
            config = initialize_a1_from_source(config, config_path)
        return cls(config)

    def with_plate_format(self, plate_format: int) -> HtsXtGeometry:
        """Return a copy using *plate_format* spacing and bounds."""
        updated = dict(self.config)
        updated["plate_format"] = int(plate_format)
        return HtsXtGeometry(updated)

    def well_position(self, well: str) -> WellPosition:
        """Resolve a well label (e.g. ``B3``) to absolute X/Y microns."""
        row, col = parse_well(well)
        max_rows, max_cols = _PLATE_DIMENSIONS.get(self.plate_format, (None, None))
        if max_rows is not None and not (0 <= row < max_rows and 0 <= col < max_cols):
            raise ValueError(
                f"Well index out of range for {self.plate_format}-well plate: "
                f"row={row}, col={col}, expected rows 0..{max_rows - 1}, "
                f"cols 0..{max_cols - 1}."
            )
        x_um = self.a1_x_um + col * self.well_spacing_um
        y_um = self.a1_y_um + row * self.well_spacing_um
        label = f"{_ROW_LABELS[row]}{col + 1}"
        return WellPosition(well=label, row=row, col=col, x_um=x_um, y_um=y_um)

    def load_plate_pose(self) -> tuple[int, int]:
        """Centered plate position used by vendor ``load_plate``."""
        return self.center_x_um, self.center_y_um

    def unload_plate_poses(self) -> list[tuple[int, int]]:
        """Center then unload coordinates (vendor ``unload_plate`` sequence)."""
        return [
            (self.center_x_um, self.center_y_um),
            (self.unload_x_um, self.unload_y_um),
        ]


class HtsXtController:
    """Moves the HTS-XT stage through a callable that accepts NOX/NOY/NDV."""

    def __init__(
        self,
        geometry: HtsXtGeometry,
        move_stage: Callable[..., Any],
    ) -> None:
        self.geometry = geometry
        self._move_stage = move_stage

    def move_to_well(self, well: str, *, plate_format: int | None = None) -> WellPosition:
        geom = (
            self.geometry.with_plate_format(plate_format)
            if plate_format is not None and plate_format > 0
            else self.geometry
        )
        pos = geom.well_position(well)
        log.info(
            "HTS-XT moving to well %s -> X=%d um, Y=%d um",
            pos.well,
            pos.x_um,
            pos.y_um,
        )
        self._move_um(pos.x_um, pos.y_um, device=geom.stage_device)
        return pos

    def load_plate(self) -> list:
        x, y = self.geometry.load_plate_pose()
        log.info("HTS-XT load: moving to center (%d, %d) um", x, y)
        return self._move_um(x, y)

    def unload_plate(self) -> list:
        responses: list = []
        for x, y in self.geometry.unload_plate_poses():
            log.info("HTS-XT unload: moving to (%d, %d) um", x, y)
            responses.extend(self._move_um(x, y) or [])
        return responses

    def _move_um(self, x_um: int, y_um: int, *, device: str | None = None) -> list:
        ndv = device if device is not None else self.geometry.stage_device
        kwargs: dict[str, Any] = {"NOX": int(x_um), "NOY": int(y_um)}
        if ndv:
            kwargs["NDV"] = ndv
        result = self._move_stage(**kwargs)
        return list(result or [])


def parse_well(well: str) -> tuple[int, int]:
    """Parse ``A1`` / ``B12`` into zero-based ``(row, col)``."""
    match = re.fullmatch(r"([A-Pa-p])(\d{1,2})", well.strip())
    if not match:
        raise ValueError(
            f"Invalid well identifier '{well}'. "
            "Expected format: letter + number, e.g. A1, B12, H3."
        )
    row = _ROW_LABELS.index(match.group(1).upper())
    col = int(match.group(2)) - 1
    return row, col


def initialize_a1_from_source(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """On first use, try to read A1 from OPUS HTS-XT.ini and persist it."""
    if config.get("a1_calibrated", False):
        return config
    if not config.get("first_use_auto_read_a1", True):
        return config

    ini_glob = config.get(
        "hts_xt_ini_glob",
        "C:/Users/Public/Documents/Bruker/OPUS_*/OpusLab/HTS-XT/HTS-XT.ini",
    )
    ini_keys = config.get(
        "hts_xt_ini_keys",
        ["REF_POS96_X_MOT_STEPS", "REF_POS96_Y_MOT_STEPS"],
    )
    ini_scale_key = config.get(
        "hts_xt_ini_microns_per_step_key", "OPUS_STAGE_MICRONS_PER_MOTORSTEP"
    )
    ini_candidates = [Path(p) for p in sorted(glob.glob(ini_glob))]
    if not ini_candidates:
        ini_candidates = [Path(ini_glob)]

    read_x, read_y = _try_read_a1_from_ini(ini_candidates, list(ini_keys), ini_scale_key)
    if read_x is None or read_y is None:
        log.info("HTS-XT first-use A1 auto-read failed; using configured defaults.")
        return config

    updated = dict(config)
    updated["a1_x_um"] = int(read_x)
    updated["a1_y_um"] = int(read_y)
    updated["a1_calibrated"] = True
    _write_config(config_path, updated)
    log.info("HTS-XT A1 loaded from ini and saved: X=%s um, Y=%s um", read_x, read_y)
    return updated


def _try_read_a1_from_ini(
    ini_candidates: list[Path], ini_keys: list[str], ini_scale_key: str
) -> tuple[Optional[int], Optional[int]]:
    parser = configparser.ConfigParser(strict=False)
    for ini_path in ini_candidates:
        if not ini_path.exists():
            continue
        try:
            parser.read(ini_path, encoding="utf-8")
        except Exception:
            continue

        x_key = ini_keys[0] if len(ini_keys) > 0 else "A1_X_UM"
        y_key = ini_keys[1] if len(ini_keys) > 1 else "A1_Y_UM"

        for section in parser.sections():
            x_raw = parser.get(section, x_key, fallback=None)
            y_raw = parser.get(section, y_key, fallback=None)
            scale_raw = parser.get(section, ini_scale_key, fallback=None)
            if x_raw is not None and y_raw is not None and scale_raw is not None:
                try:
                    scale = float(scale_raw)
                    x_um = int(round(float(x_raw) * scale))
                    y_um = int(round(float(y_raw) * scale))
                    return x_um, y_um
                except ValueError:
                    continue
    return None, None


def _write_config(config_path: Path, config: dict[str, Any]) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
        f.write("\n")
