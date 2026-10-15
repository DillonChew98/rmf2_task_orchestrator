"""Curated bindings for OPUS file and data block management functions.

Pass OPUS 3-letter parameter codes as keyword arguments; use
``ftir.opus.describe("SaveAs")`` to inspect available codes. Enum members from
:mod:`ftir_automation.models.opus_params` (e.g. :class:`SaveAsMode`) are
accepted and coerced to wire values automatically.
"""

from __future__ import annotations

import ntpath
from typing import Any, Optional, Union

from ftir_automation.models.opus_params import (
    DataPointsMode,
    OrdinateMode,
    OverwriteMode,
    SaveAsMode,
    normalize_opus_params,
)
from ftir_automation.opus_bindings.proxy import _CommandGroup

FileId = Optional[Union[int, list]]
BlockName = Optional[Union[str, list]]


class FileCommands(_CommandGroup):
    """OPUS file lifecycle, export, plotting, and data block management."""

    def load(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Load — load a spectrum file into the workspace."""
        return self._fn.Load(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def unload(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Unload — remove a file from the workspace."""
        return self._fn.Unload(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def unload_all(self, **params: Any) -> list:
        """Run OPUS UnloadAll — remove all files from the workspace."""
        return self._fn.UnloadAll(**normalize_opus_params(params))

    def save(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Save — save the file in place."""
        return self._fn.Save(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def save_as(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SaveAs — save under a new name/path or export format.

        Common params:
          - ``SAN`` / ``DAP`` (or ``NAM`` / ``PTH``): file name and directory
          - ``COF``: :class:`SaveAsMode` bit flags (e.g. ``SaveAsMode.DATA_POINT_TABLE``)
          - ``OEX``: :class:`OverwriteMode`
          - For datapoint tables: ``DPA``, ``DPO``, ``SEP``, ``YON``, ``ADP``
        """
        return self._fn.SaveAs(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def save_datapoint_table(
        self,
        output_path: str,
        file_id: FileId = 0,
        block_name: BlockName = None,
        *,
        methods_path: str = "",
        overwrite: OverwriteMode = OverwriteMode.INCREMENT_NAME,
        abscissa_decimals: int = 5,
        ordinate_decimals: int = 5,
        separator: str = ",",
        ordinate_mode: OrdinateMode = OrdinateMode.XY_VALUES,
        data_points: DataPointsMode = DataPointsMode.ALL_POINTS,
        extra_mode: SaveAsMode | int = 0,
        **params: Any,
    ) -> list:
        """Export the current spectrum as an OPUS datapoint table (``.dpt``).

        Equivalent to ``SaveAs`` with ``COF=SaveAsMode.DATA_POINT_TABLE`` (64),
        matching the HTS-XT workflow that writes comma-separated x,y tables.
        """
        directory, filename = ntpath.split(output_path)
        stem = ntpath.splitext(filename)[0] if filename else ""
        payload: dict[str, Any] = {
            "DAP": directory or ".",
            "SAN": stem,
            "COF": SaveAsMode.DATA_POINT_TABLE | SaveAsMode(extra_mode),
            "OEX": overwrite,
            "DPA": abscissa_decimals,
            "DPO": ordinate_decimals,
            "SEP": separator,
            "YON": ordinate_mode,
            "ADP": data_points,
        }
        if methods_path:
            payload["INP"] = methods_path
            payload["IFP"] = methods_path
            payload.setdefault("INM", "DEFAULT")
            payload.setdefault("IFN", "DEFAULT")
        payload.update(params)
        return self.save_as(file_id, block_name, **payload)

    def restore(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Restore — revert the file to its saved state."""
        return self._fn.Restore(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def export_spectra(self, **params: Any) -> list:
        """Run OPUS ExportSpectra — export to external formats (CSV, JCAMP-DX, …)."""
        return self._fn.ExportSpectra(**normalize_opus_params(params))

    def plot(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Plot — plot the spectrum (e.g. to a printer/file)."""
        return self._fn.Plot(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def print_spectrum(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS Print — print a report/spectrum."""
        return self._fn.Print(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def copy_data_block(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS CopyDataBlock — copy a data block between files."""
        return self._fn.CopyDataBlock(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def delete_data_block(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS DeleteDataBlock — delete a data block from a file."""
        return self._fn.DeleteDataBlock(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def change_data_block_type(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS ChangeDataBlockType — change a block's type designation."""
        return self._fn.ChangeDataBlockType(file_id=file_id, block_name=block_name, **normalize_opus_params(params))

    def save_modified(self, file_id: FileId = None, block_name: BlockName = None, **params: Any) -> list:
        """Run OPUS SaveModified — save all modified files."""
        return self._fn.SaveModified(file_id=file_id, block_name=block_name, **normalize_opus_params(params))
