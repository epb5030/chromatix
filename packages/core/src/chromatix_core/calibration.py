"""Calibration curves (response vs amount) and back-calculation of unknowns."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike

Model = Literal["linear", "quadratic"]
Weighting = Literal["none", "1/x", "1/x^2"]


@dataclass(frozen=True)
class CalibrationCurve:
    model: Model
    coefficients: tuple[float, ...]
    """Polynomial coefficients, highest power first (``a, b, c`` for ``a x^2 + b x + c``)."""
    r_squared: float
    weighting: Weighting
    through_origin: bool
    amount_range: tuple[float, float]

    def response(self, amount: float) -> float:
        return float(np.polyval(self.coefficients, amount))

    def validate_monotonic(self) -> None:
        """Reject a quadratic with a turning point strictly inside the calibrated range."""
        if self.model == "quadratic":
            a, b, _ = self.coefficients
            lo, hi = self.amount_range
            if a != 0 and lo < (-0.5 * b) / a < hi:
                raise ValueError("quadratic calibration turns within the calibrated amount range")

    def amount(self, response: float) -> float:
        """Back-calculate the amount that gives ``response``."""
        if self.model == "linear":
            slope, intercept = self.coefficients
            if slope == 0:
                raise ValueError("calibration slope is zero")
            return (response - intercept) / slope
        a, b, c = self.coefficients
        if a == 0:
            return (response - c) / b
        self.validate_monotonic()
        lo, hi = self.amount_range
        roots = np.roots([a, b, c - response])
        real = sorted(float(r.real) for r in roots if abs(r.imag) < 1e-12)
        if not real:
            raise ValueError(f"response {response} is outside the quadratic curve")
        span = hi - lo
        inside = [r for r in real if lo - span <= r <= hi + span]
        candidates = inside or real
        mid = (lo + hi) / 2
        return min(candidates, key=lambda r: abs(r - mid))


def fit_calibration(
    amounts: ArrayLike,
    responses: ArrayLike,
    model: Model = "linear",
    weighting: Weighting = "none",
    through_origin: bool = False,
) -> CalibrationCurve:
    """Least-squares calibration fit.

    ``weighting`` applies 1/x or 1/x^2 weights to the squared residuals.
    ``r_squared`` is the ordinary (unweighted) coefficient of determination of
    the fitted curve.
    """
    x = np.asarray(amounts, dtype=np.float64)
    y = np.asarray(responses, dtype=np.float64)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("amounts and responses must be 1-D arrays of equal length")
    degree = 1 if model == "linear" else 2
    n_params = degree + (0 if through_origin else 1)
    if x.size < n_params + 1:
        raise ValueError(f"{model} fit needs at least {n_params + 1} points")
    if weighting != "none" and np.any(x <= 0):
        raise ValueError("1/x weighting needs positive amounts")

    powers = list(range(degree, -1 if not through_origin else 0, -1))
    design = np.column_stack([x**k for k in powers])
    if weighting == "none":
        sw = np.ones_like(x)
    elif weighting == "1/x":
        sw = np.sqrt(1 / x)
    else:
        sw = 1 / x
    solution, _, rank, _ = np.linalg.lstsq(design * sw[:, None], y * sw, rcond=None)
    if rank < n_params:
        raise ValueError(f"{model} calibration fit is rank-deficient")
    coeffs = [float(c) for c in solution]
    if through_origin:
        coeffs.append(0.0)

    fitted = np.polyval(coeffs, x)
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else math.nan
    return CalibrationCurve(
        model=model,
        coefficients=tuple(coeffs),
        r_squared=r2,
        weighting=weighting,
        through_origin=through_origin,
        amount_range=(float(x.min()), float(x.max())),
    )
