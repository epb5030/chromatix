"""AIA / ANDI chromatography files (ASTM E1947, netCDF).

This is the vendor-neutral format Empower, Chromeleon, OpenLab and others can
export.  The standard stores the detector trace as ``ordinate_values`` and,
for uniformly sampled data, *no time array*: time is rebuilt from
``actual_delay_time`` and ``actual_sampling_interval`` (both in seconds).
Non-uniform data carries ``raw_data_retention`` (seconds) instead.
"""

from __future__ import annotations

from datetime import datetime
from os import PathLike
from typing import Any

import netCDF4
import numpy as np

from chromatix_core.chromatogram import Chromatogram

_GLOBAL_METADATA = (
    "sample_name",
    "sample_id",
    "sample_type",
    "operator_name",
    "injection_date_time_stamp",
    "detection_method_name",
    "experiment_title",
    "dataset_origin",
)


def _scalar(nc: netCDF4.Dataset, name: str) -> float | None:
    if name not in nc.variables:
        return None
    value = np.asarray(nc.variables[name][:], dtype=np.float64)
    return float(value.reshape(-1)[0]) if value.size else None


def read_aia(path: str | PathLike[str]) -> Chromatogram:
    """Read an ASTM E1947 chromatography file into a :class:`Chromatogram`."""
    with netCDF4.Dataset(path, "r") as nc:
        nc.set_auto_mask(False)
        if "ordinate_values" not in nc.variables:
            raise ValueError(f"{path}: not an AIA chromatogram (no ordinate_values)")
        signal = np.asarray(nc.variables["ordinate_values"][:], dtype=np.float64)

        if "raw_data_retention" in nc.variables:
            seconds = np.asarray(nc.variables["raw_data_retention"][:], dtype=np.float64)
        else:
            interval = _scalar(nc, "actual_sampling_interval")
            if interval is None or interval <= 0:
                raise ValueError(f"{path}: missing or invalid actual_sampling_interval")
            delay = _scalar(nc, "actual_delay_time") or 0.0
            seconds = delay + interval * np.arange(signal.size, dtype=np.float64)

        attrs = {k: nc.getncattr(k) for k in nc.ncattrs()}
        metadata: dict[str, Any] = {k: attrs[k] for k in _GLOBAL_METADATA if k in attrs}
        metadata["format"] = "aia"
        return Chromatogram(
            time=seconds / 60.0,
            signal=signal,
            signal_unit=str(attrs.get("detector_unit", "")),
            detector=str(attrs.get("detector_name", "")),
            metadata=metadata,
        )


def write_aia(chrom: Chromatogram, path: str | PathLike[str]) -> None:
    """Write a chromatogram as an ASTM E1947 file.

    Uniformly sampled data is written the standard way (interval + delay, no
    time array); anything else gets ``raw_data_retention``.
    """
    seconds = chrom.time * 60.0
    steps = np.diff(seconds)
    uniform = bool(np.allclose(steps, steps[0], rtol=1e-6, atol=1e-9))
    with netCDF4.Dataset(path, "w", format="NETCDF3_CLASSIC") as nc:
        nc.dataset_completeness = "C1+C2"
        nc.aia_template_revision = "1.0"
        nc.dataset_date_time_stamp = datetime.now().strftime("%Y%m%d%H%M%S%z")
        nc.detector_name = chrom.detector
        nc.detector_unit = chrom.signal_unit
        for key in _GLOBAL_METADATA:
            if key in chrom.metadata:
                nc.setncattr(key, str(chrom.metadata[key]))
        nc.createDimension("point_number", len(chrom))
        ordinate = nc.createVariable("ordinate_values", "f8", ("point_number",))
        ordinate[:] = chrom.signal
        ordinate.uniform_sampling_flag = "Y" if uniform else "N"
        for name, value in (
            ("actual_sampling_interval", float(steps[0]) if uniform else float(np.median(steps))),
            ("actual_delay_time", float(seconds[0])),
            ("actual_run_time_length", float(seconds[-1] - seconds[0])),
        ):
            var = nc.createVariable(name, "f8")
            var.assignValue(value)
        if not uniform:
            retention = nc.createVariable("raw_data_retention", "f8", ("point_number",))
            retention[:] = seconds
