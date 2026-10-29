import pytest

from chromatix_core import IntegrationParams, integrate
from chromatix_core.quant import (
    CalibrationPoint,
    Component,
    ProcessingMethod,
    calibrate,
    identify,
)
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram

CAFFEINE = Component("Caffeine", 2.0, window=0.1, unit="µg/mL")
THEOBROMINE = Component("Theobromine", 4.0, window=0.1, unit="µg/mL")


class P:
    def __init__(self, rt: float) -> None:
        self.retention_time = rt


def test_identify_nearest_within_window():
    peaks = [P(1.95), P(2.04), P(3.5), P(4.02)]
    assert identify(peaks, [CAFFEINE, THEOBROMINE]) == {1: "Caffeine", 3: "Theobromine"}


def test_identify_ignores_peaks_outside_window():
    assert identify([P(2.2)], [CAFFEINE]) == {}


def test_identify_each_peak_used_once():
    a, b = Component("A", 2.0, window=0.2), Component("B", 2.05, window=0.2)
    assignment = identify([P(2.06)], [a, b])
    assert assignment == {0: "B"}  # B is closer; A gets nothing rather than sharing


def test_method_round_trip_and_legacy_records():
    method = ProcessingMethod(IntegrationParams(threshold_snr=15), (CAFFEINE, THEOBROMINE))
    assert ProcessingMethod.from_dict(method.to_dict()) == method
    legacy = {"threshold_snr": 12.0, "smoothing_window": None}
    old = ProcessingMethod.from_dict(legacy)
    assert old.integration.threshold_snr == 12.0
    assert old.components == ()


def test_method_validation():
    with pytest.raises(ValueError, match="unique"):
        ProcessingMethod(components=(CAFFEINE, CAFFEINE))
    with pytest.raises(ValueError, match="window"):
        Component("X", 1.0, window=0)


def test_calibrate_and_quantitate_synthetic_series():
    """Standards with peak height proportional to amount recover an unknown amount."""
    sigma, response_per_unit = 0.05, 40.0  # height per µg/mL

    def run(amount: float, seed: int):
        chrom = gaussian_chromatogram(
            [GaussianPeak(2.0, response_per_unit * amount, sigma)],
            offset=2.0,
            drift=0.5,
            noise=0.3,
            seed=seed,
        )
        peaks = integrate(chrom)
        (index,) = [i for i, name in identify(peaks, [CAFFEINE]).items() if name == "Caffeine"]
        return peaks[index].area

    points = [CalibrationPoint(a, run(a, seed)) for seed, a in enumerate((0.5, 1, 2, 4, 8))]
    curve = calibrate(CAFFEINE, points)
    assert curve.r_squared > 0.9995
    assert curve.amount(run(3.0, seed=99)) == pytest.approx(3.0, rel=0.02)


def test_calibrate_needs_points():
    with pytest.raises(ValueError, match="no calibration points"):
        calibrate(CAFFEINE, [])
