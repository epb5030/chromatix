"""Peak detection and integration.

Approach (deliberately simple and verifiable):

1. Estimate the noise from the raw signal (robust, from first differences).
2. Smooth with Savitzky-Golay and take its first derivative.  Unless a window
   is given, it is sized from the data: half the half-height width of the
   narrowest candidate peak, so it adapts to sampling rate and column.
3. Apexes are local maxima of the smoothed trace whose prominence exceeds
   ``threshold_snr`` x noise.
4. Each apex's start/end is found by walking outward until the smoothed slope
   has settled back to the background slope for a run of points (lift-off /
   touch-down), then extended outward by ``boundary_extension`` peak standard
   deviations so the baseline is anchored past the tail, not on it.  Walks
   never cross the valley to a neighbouring apex.
5. Adjacent peaks are merged into one cluster when the signal at the valley
   between them is clearly above their common baseline; inside a cluster the
   peaks are split with vertical drop lines at the valleys.
6. Each cluster gets a straight baseline between its start and end, and peak
   areas are trapezoidal integrals of (raw signal - baseline).

Boundary codes follow the usual CDS convention: ``B`` = baseline, ``V`` =
valley (drop line), so a peak is ``BB``, ``BV``, ``VB`` or ``VV``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
from numpy.typing import NDArray
from scipy.signal import find_peaks, peak_widths, savgol_filter

from chromatix_core.chromatogram import Chromatogram, FloatArray

_MAD_TO_SIGMA = 1.4826
_FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


@dataclass(frozen=True)
class IntegrationParams:
    """Processing-method parameters for :func:`integrate`."""

    threshold_snr: float = 10.0
    """Minimum peak prominence, as a multiple of the baseline noise."""
    smoothing_window: float | None = None
    """Savitzky-Golay window in minutes (odd number of points >= 5).  ``None``
    sizes it automatically from the narrowest peak's half-height width."""
    smoothing_fraction: float = 0.5
    """Automatic window = this fraction of the narrowest peak's half-height width."""
    boundary_extension: float = 1.0
    """Baseline-type boundaries are pushed outward by this many peak standard
    deviations (estimated as FWHM / 2.355)."""
    slope_sensitivity: float = 3.0
    """A point counts as baseline when its slope is within this many slope-noise
    standard deviations of the background slope."""
    valley_snr: float = 5.0
    """Peaks are merged into a cluster when the valley between them sits more than
    this multiple of the noise above their shared baseline."""
    min_area: float = 0.0
    min_height: float = 0.0


@dataclass(frozen=True)
class Peak:
    retention_time: float
    start_time: float
    end_time: float
    height: float
    area: float
    width_50: float
    """Full width at half height in minutes (NaN if the peak never drops to half height)."""
    boundary: str
    """Two-letter code: ``BB``, ``BV``, ``VB`` or ``VV``."""
    apex_index: int
    start_index: int
    end_index: int
    baseline_start: float
    baseline_end: float


def estimate_noise(signal: FloatArray) -> float:
    """Robust standard deviation of white baseline noise.

    Uses the median absolute deviation of first differences, which ignores
    slow drift and the (sparse) peaks.  ``std(diff) = sqrt(2) * sigma``.
    """
    d = np.diff(np.asarray(signal, dtype=np.float64))
    mad = float(np.median(np.abs(d - np.median(d))))
    return _MAD_TO_SIGMA * mad / math.sqrt(2.0)


def _odd_window(points: float, n: int) -> int:
    w = max(5, round(points))
    if w % 2 == 0:
        w += 1
    largest = n if n % 2 else n - 1
    return min(w, largest)


def integrate(chrom: Chromatogram, params: IntegrationParams | None = None) -> list[Peak]:
    """Detect and integrate the peaks in ``chrom``."""
    p = params or IntegrationParams()
    t, y = chrom.time, chrom.signal
    n = len(chrom)
    dt = chrom.sampling_interval

    # Floor the noise so noise-free (synthetic or heavily filtered) data does not
    # turn floating-point ripple into peaks.
    noise = max(estimate_noise(y), 1e-6 * float(np.ptp(y)), float(np.finfo(np.float64).tiny))
    prominence = p.threshold_snr * noise

    if p.smoothing_window is not None:
        window = _odd_window(p.smoothing_window / dt, n)
    else:
        rough = savgol_filter(y, _odd_window(5, n), 2)
        candidates, _ = find_peaks(rough, prominence=prominence)
        if candidates.size == 0:
            return []
        fwhm = peak_widths(rough, candidates, rel_height=0.5)[0]
        window = _odd_window(p.smoothing_fraction * float(np.min(fwhm)), n)

    smooth = savgol_filter(y, window, 2)
    found, _ = find_peaks(smooth, prominence=prominence)
    apexes = [int(a) for a in found]
    if not apexes:
        return []
    fwhm_pts = peak_widths(smooth, found, rel_height=0.5)[0]
    sigma_pts = fwhm_pts / _FWHM_PER_SIGMA
    valleys = [a + int(np.argmin(smooth[a : b + 1])) for a, b in pairwise(apexes)]

    # Boundaries of each peak are judged on a slope trace smoothed to that peak's
    # own width, so broad peaks are not cut short by slope noise.
    flat_cache: dict[int, tuple[NDArray[np.bool_], int]] = {}

    def flat_for(w: int) -> tuple[NDArray[np.bool_], int]:
        if w not in flat_cache:
            slope = savgol_filter(y, w, 2, deriv=1, delta=dt)
            bg_slope = float(np.median(slope))
            deviation = np.abs(slope - bg_slope)
            slope_noise = _MAD_TO_SIGMA * float(np.median(deviation))
            # Floor for noise-free data: "flat" = within 0.01 % of the steepest slope.
            floor = max(1e-4 * float(np.max(deviation)), float(np.finfo(np.float64).tiny))
            tol = p.slope_sensitivity * max(slope_noise, floor)
            flat_cache[w] = (np.abs(slope - bg_slope) <= tol, max(3, w // 2))
        return flat_cache[w]

    def walk(apex: int, limit: int, step: int, extend: int, w: int) -> tuple[int, str]:
        flat, hold = flat_for(w)
        i = apex
        while i != limit and flat[i]:  # leave the flat top of the peak
            i += step
        run = 0
        while i != limit:
            if flat[i]:
                run += 1
                if run >= hold:
                    bound = i - step * (hold - 1) + step * extend
                    bound = max(bound, limit) if step < 0 else min(bound, limit)
                    return bound, "B"
            else:
                run = 0
            i += step
        is_valley = (step < 0 and limit != 0) or (step > 0 and limit != n - 1)
        return limit, "V" if is_valley else "B"

    starts: list[tuple[int, str]] = []
    ends: list[tuple[int, str]] = []
    for k, apex in enumerate(apexes):
        extend = round(p.boundary_extension * float(sigma_pts[k]))
        w = (
            window
            if p.smoothing_window is not None
            else _odd_window(p.smoothing_fraction * float(fwhm_pts[k]), n)
        )
        starts.append(walk(apex, valleys[k - 1] if k > 0 else 0, -1, extend, w))
        ends.append(walk(apex, valleys[k] if k < len(valleys) else n - 1, +1, extend, w))

    # Reconcile each valley so both neighbours agree: if the valley is clearly
    # above the baseline the pair would share, split with a drop line (V/V);
    # otherwise the signal is back at baseline, so any side that ran into the
    # valley ends there as a baseline bound.
    for k in range(len(apexes) - 1):
        a, b, v = starts[k][0], ends[k + 1][0], valleys[k]
        line_v = smooth[a] + (smooth[b] - smooth[a]) * (t[v] - t[a]) / (t[b] - t[a])
        if smooth[v] - line_v > p.valley_snr * noise:
            ends[k] = (v, "V")
            starts[k + 1] = (v, "V")
        else:
            if ends[k][1] == "V":
                ends[k] = (v, "B")
            if starts[k + 1][1] == "V":
                starts[k + 1] = (v, "B")

    # Group into clusters joined by valley drop lines.
    clusters: list[list[int]] = [[0]]
    for k in range(1, len(apexes)):
        if starts[k][1] == "V":
            clusters[-1].append(k)
        else:
            clusters.append([k])

    peaks: list[Peak] = []
    for cluster in clusters:
        c0, c1 = starts[cluster[0]][0], ends[cluster[-1]][0]
        b0, b1 = float(smooth[c0]), float(smooth[c1])

        def baseline(
            idx: NDArray[np.intp] | int,
            c0: int = c0,
            c1: int = c1,
            b0: float = b0,
            b1: float = b1,
        ) -> FloatArray:
            frac = (t[idx] - t[c0]) / (t[c1] - t[c0]) if c1 > c0 else 0.0 * t[idx]
            return np.asarray(b0 + (b1 - b0) * frac, dtype=np.float64)

        for k in cluster:
            (s, s_type), (e, e_type), apex = starts[k], ends[k], apexes[k]
            idx = np.arange(s, e + 1)
            corrected = y[idx] - baseline(idx)
            area = float(np.trapezoid(corrected, t[idx]))
            height = float(smooth[apex] - baseline(apex))
            rt = _refine_apex(t, smooth, apex)
            width = _width_at_fraction(t[idx], smooth[idx] - baseline(idx), height, 0.5)
            if area < p.min_area or height < p.min_height:
                continue
            peaks.append(
                Peak(
                    retention_time=rt,
                    start_time=float(t[s]),
                    end_time=float(t[e]),
                    height=height,
                    area=area,
                    width_50=width,
                    boundary=s_type + e_type,
                    apex_index=apex,
                    start_index=s,
                    end_index=e,
                    baseline_start=float(baseline(s)),
                    baseline_end=float(baseline(e)),
                )
            )
    return peaks


def _refine_apex(t: FloatArray, y: FloatArray, i: int) -> float:
    """Parabolic interpolation of the apex time from the three points around ``i``."""
    if i <= 0 or i >= len(y) - 1:
        return float(t[i])
    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    denom = y0 - 2 * y1 + y2
    if denom == 0:
        return float(t[i])
    offset = 0.5 * (y0 - y2) / denom
    return float(t[i] + offset * (t[i + 1] - t[i - 1]) / 2)


def _width_at_fraction(t: FloatArray, y: FloatArray, height: float, fraction: float) -> float:
    level = height * fraction
    above = np.nonzero(y >= level)[0]
    if above.size == 0 or above[0] == 0 or above[-1] == y.size - 1:
        return math.nan
    i0, i1 = int(above[0]), int(above[-1])
    left = float(np.interp(level, [y[i0 - 1], y[i0]], [t[i0 - 1], t[i0]]))
    right = float(np.interp(level, [y[i1 + 1], y[i1]], [t[i1 + 1], t[i1]]))
    return right - left
