"""Accuracy against synthetic chromatograms whose true peak areas are known exactly.

Tolerances come from a 200-seed sweep (noise 0.3, drift 0.5/min, 10 Hz) on
2026-10-29: worst-case area errors were 0.8 %, 1.8 % and 5.8 % for the three
isolated peaks (the last has a signal-to-noise ratio of ~33), and 1.4 % / 2.0 %
for the pair relative to its ideal drop-line split.  All means were within
0.1 % of zero.  Per-seed limits below are ~1.3x those worst cases; the bias
test checks that errors average out rather than accumulate.
"""

import math

import numpy as np
import pytest
from scipy.integrate import trapezoid

from chromatix_core import IntegrationParams, estimate_noise, integrate
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram

ISOLATED = [GaussianPeak(2.0, 100, 0.05), GaussianPeak(4.0, 40, 0.04), GaussianPeak(7.5, 10, 0.08)]
ISOLATED_REL_TOL = [0.011, 0.025, 0.08]
# Resolution Rs = dt / (2 * (s1 + s2)) = 0.25 / 0.24 ~= 1.04: valley well above baseline.
PAIR = [GaussianPeak(5.0, 60, 0.06), GaussianPeak(5.25, 30, 0.06)]
NOISY = {"offset": 2.0, "drift": 0.5, "noise": 0.3}


def _sorted(peaks):
    return sorted(peaks, key=lambda p: p.retention_time)


def _ideal_drop_split(peaks):
    """Areas a perfect vertical drop at the valley would give on noise-free data."""
    chrom = gaussian_chromatogram(peaks)
    t, y = chrom.time, chrom.signal
    lo, hi = peaks[0].retention_time, peaks[1].retention_time
    v = int(np.argmin(np.where((t > lo) & (t < hi), y, np.inf)))
    return trapezoid(y[: v + 1], t[: v + 1]), trapezoid(y[v:], t[v:])


def test_isolated_peaks_clean():
    found = _sorted(integrate(gaussian_chromatogram(ISOLATED)))
    assert len(found) == 3
    for peak, truth in zip(found, ISOLATED, strict=True):
        assert peak.boundary == "BB"
        assert peak.retention_time == pytest.approx(truth.retention_time, abs=0.001)
        assert peak.area == pytest.approx(truth.area, rel=1e-3)
        # Height is read from the smoothed trace, which flattens narrow apexes slightly.
        assert peak.height == pytest.approx(truth.height, rel=5e-3)


@pytest.mark.parametrize("seed", range(10))
def test_isolated_peaks_with_drift_and_noise(seed):
    found = _sorted(integrate(gaussian_chromatogram(ISOLATED, seed=seed, **NOISY)))
    assert len(found) == 3
    for peak, truth, tol in zip(found, ISOLATED, ISOLATED_REL_TOL, strict=True):
        assert peak.boundary == "BB"
        assert peak.retention_time == pytest.approx(truth.retention_time, abs=0.02)
        assert peak.area == pytest.approx(truth.area, rel=tol)


def test_isolated_areas_are_unbiased():
    errors = np.array(
        [
            [
                p.area / t.area - 1
                for p, t in zip(
                    _sorted(integrate(gaussian_chromatogram(ISOLATED, seed=s, **NOISY))),
                    ISOLATED,
                    strict=True,
                )
            ]
            for s in range(50)
        ]
    )
    assert np.all(np.abs(errors.mean(axis=0)) < [0.002, 0.004, 0.01])


def test_half_height_width():
    chrom = gaussian_chromatogram([GaussianPeak(3.0, 50, 0.05)], offset=1.0, drift=0.2)
    (peak,) = integrate(chrom)
    assert peak.width_50 == pytest.approx(2 * math.sqrt(2 * math.log(2)) * 0.05, rel=0.02)


@pytest.mark.parametrize("seed", range(10))
def test_partially_resolved_pair_uses_drop_line(seed):
    found = _sorted(integrate(gaussian_chromatogram(PAIR, seed=seed, **NOISY)))
    assert [p.boundary for p in found] == ["BV", "VB"]
    first, second = found
    assert first.end_index == second.start_index
    assert first.area + second.area == pytest.approx(sum(t.area for t in PAIR), rel=0.015)
    for peak, ideal, tol in zip(found, _ideal_drop_split(PAIR), (0.02, 0.03), strict=True):
        assert peak.area == pytest.approx(ideal, rel=tol)


@pytest.mark.parametrize("seed", range(10))
def test_noise_only_gives_no_peaks(seed):
    assert integrate(gaussian_chromatogram([], seed=seed, **NOISY)) == []


def test_noise_estimate_ignores_drift_and_peaks():
    chrom = gaussian_chromatogram(ISOLATED, offset=5.0, drift=1.0, noise=0.3, seed=1)
    assert estimate_noise(chrom.signal) == pytest.approx(0.3, rel=0.1)


def test_threshold_controls_small_peaks():
    chrom = gaussian_chromatogram([GaussianPeak(3.0, 2.0, 0.05)], noise=0.3, seed=3)  # ~7x noise
    assert integrate(chrom) == []
    # At low thresholds automatic window sizing is fooled by noise spikes, so a
    # method that hunts for trace peaks should fix the smoothing window.
    low = IntegrationParams(threshold_snr=4.0, smoothing_window=0.06)
    (peak,) = integrate(chrom, low)
    assert peak.retention_time == pytest.approx(3.0, abs=0.02)


def test_fixed_smoothing_window():
    chrom = gaussian_chromatogram(ISOLATED, seed=2, **NOISY)
    found = _sorted(integrate(chrom, IntegrationParams(smoothing_window=0.05)))
    assert [round(p.retention_time, 1) for p in found] == [2.0, 4.0, 7.5]


def test_min_area_filter():
    found = integrate(gaussian_chromatogram(ISOLATED), IntegrationParams(min_area=5.0))
    assert [round(p.retention_time, 1) for p in found] == [2.0]


def test_result_is_deterministic():
    chrom = gaussian_chromatogram(ISOLATED + PAIR, noise=0.3, seed=9)
    a, b = integrate(chrom), integrate(chrom)
    assert a == b
    assert np.isfinite([p.area for p in a]).all()
