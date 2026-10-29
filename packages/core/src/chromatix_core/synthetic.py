"""Synthetic chromatograms with exactly known peak areas, for tests and demos."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from chromatix_core.chromatogram import Chromatogram


@dataclass(frozen=True)
class GaussianPeak:
    retention_time: float
    height: float
    sigma: float

    @property
    def area(self) -> float:
        return self.height * self.sigma * math.sqrt(2 * math.pi)


def gaussian_chromatogram(
    peaks: Sequence[GaussianPeak],
    run_time: float = 10.0,
    rate_hz: float = 10.0,
    offset: float = 0.0,
    drift: float = 0.0,
    noise: float = 0.0,
    seed: int = 0,
) -> Chromatogram:
    """Sum of Gaussians on a linear baseline (``offset + drift * t``) plus white noise."""
    n = round(run_time * 60 * rate_hz) + 1
    t = np.linspace(0.0, run_time, n)
    y = offset + drift * t
    for pk in peaks:
        y = y + pk.height * np.exp(-0.5 * ((t - pk.retention_time) / pk.sigma) ** 2)
    if noise:
        y = y + np.random.default_rng(seed).normal(0.0, noise, n)
    return Chromatogram(time=t, signal=y, signal_unit="mAU", detector="synthetic")
