import numpy as np
import pytest

from chromatix_core import fit_calibration

AMOUNTS = np.array([0.1, 0.5, 1.0, 2.0, 5.0])


def test_linear_exact():
    curve = fit_calibration(AMOUNTS, 1500 * AMOUNTS + 20)
    assert curve.coefficients == pytest.approx((1500, 20))
    assert curve.r_squared == pytest.approx(1.0)
    assert curve.amount(3020) == pytest.approx(2.0)


def test_through_origin():
    curve = fit_calibration(AMOUNTS, 1500 * AMOUNTS + 20, through_origin=True)
    assert curve.coefficients[1] == 0.0
    assert curve.response(0.0) == 0.0


def test_weighting_favours_low_standards():
    responses = 1000 * AMOUNTS
    responses[-1] *= 1.05  # 5 % error on the top standard only
    plain = fit_calibration(AMOUNTS, responses)
    weighted = fit_calibration(AMOUNTS, responses, weighting="1/x^2")
    low = 0.1
    err_plain = abs(plain.amount(1000 * low) - low)
    err_weighted = abs(weighted.amount(1000 * low) - low)
    assert err_weighted < err_plain


def test_quadratic_back_calculation():
    responses = -20 * AMOUNTS**2 + 1500 * AMOUNTS + 10
    curve = fit_calibration(AMOUNTS, responses, model="quadratic")
    assert curve.coefficients == pytest.approx((-20, 1500, 10))
    for x in (0.3, 1.7, 4.2):
        assert curve.amount(curve.response(x)) == pytest.approx(x)


def test_too_few_points():
    with pytest.raises(ValueError, match="at least"):
        fit_calibration([1, 2], [3, 4], model="linear")
