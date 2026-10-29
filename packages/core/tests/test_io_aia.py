import netCDF4
import numpy as np
import pytest

from chromatix_core import Chromatogram
from chromatix_core.io import read_aia, write_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram


def test_standard_file_without_time_array(tmp_path):
    """A typical E1947 export: ordinate_values + sampling interval, no time array."""
    path = tmp_path / "standard.cdf"
    y = 100 * np.exp(-0.5 * ((np.arange(3000) * 0.2 - 120) / 3) ** 2)
    with netCDF4.Dataset(path, "w", format="NETCDF3_CLASSIC") as nc:
        nc.detector_name = "PDA 254 nm"
        nc.detector_unit = "mAU"
        nc.sample_name = "Std 1"
        nc.createDimension("point_number", y.size)
        nc.createVariable("ordinate_values", "f4", ("point_number",))[:] = y
        for name, value in (("actual_sampling_interval", 0.2), ("actual_delay_time", 0.0)):
            nc.createVariable(name, "f4").assignValue(value)

    chrom = read_aia(path)
    assert len(chrom) == 3000
    assert chrom.time[np.argmax(chrom.signal)] == pytest.approx(2.0)
    assert chrom.sampling_interval == pytest.approx(0.2 / 60)
    assert chrom.detector == "PDA 254 nm"
    assert chrom.signal_unit == "mAU"
    assert chrom.metadata["sample_name"] == "Std 1"


def test_round_trip_uniform(tmp_path):
    original = gaussian_chromatogram([GaussianPeak(2.0, 50, 0.05)], noise=0.1)
    path = tmp_path / "rt.cdf"
    write_aia(original, path)
    with netCDF4.Dataset(path) as nc:
        assert "raw_data_retention" not in nc.variables
    back = read_aia(path)
    np.testing.assert_allclose(back.time, original.time, atol=1e-9)
    np.testing.assert_allclose(back.signal, original.signal)


def test_round_trip_non_uniform(tmp_path):
    t = np.cumsum(np.full(500, 0.01) + np.linspace(0, 0.005, 500))
    original = Chromatogram.from_arrays(t, np.sin(t))
    path = tmp_path / "nu.cdf"
    write_aia(original, path)
    back = read_aia(path)
    np.testing.assert_allclose(back.time, original.time, rtol=1e-12)


def test_rejects_non_chromatogram(tmp_path):
    path = tmp_path / "other.cdf"
    with netCDF4.Dataset(path, "w", format="NETCDF3_CLASSIC") as nc:
        nc.createDimension("scan_number", 3)
        nc.createVariable("scan_acquisition_time", "f8", ("scan_number",))[:] = [0, 1, 2]
    with pytest.raises(ValueError, match="ordinate_values"):
        read_aia(path)


def test_chromatogram_is_read_only_and_validated():
    chrom = Chromatogram.from_arrays([0, 1, 2], [1, 2, 3])
    with pytest.raises(ValueError):
        chrom.signal[0] = 9
    with pytest.raises(ValueError, match="increasing"):
        Chromatogram.from_arrays([0, 2, 1], [1, 2, 3])
    with pytest.raises(ValueError, match="lengths"):
        Chromatogram.from_arrays([0, 1, 2], [1, 2])
