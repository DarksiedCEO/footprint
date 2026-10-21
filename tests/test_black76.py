"""Black-76 (futures option) pricing: price and Greeks, tested before the code exists.

Reference values:
- Hull, "Options, Futures, and Other Derivatives", the Black-model futures option
  example: F = K = 20, r = 9%, sigma = 25%, T = 4 months -> a European put worth about
  1.12 (at the money, the call is worth the same).
- Put-call parity for futures options: C - P = exp(-rT) * (F - K).
- Every Greek must match a finite difference of the price (the Greeks are the price's
  derivatives, so this pins each formula independently of how it is written).
"""

from __future__ import annotations

import math

import pytest

from footprint.core.black76 import Black76, Right76, greeks, price

HULL = {"forward": 20.0, "strike": 20.0, "years": 4 / 12, "vol": 0.25, "rate": 0.09}


# --- reference prices -------------------------------------------------------


def test_hull_put_example() -> None:
    assert price(Right76.PUT, **HULL) == pytest.approx(1.1166, abs=5e-4)


def test_at_the_money_call_equals_put() -> None:
    call = price(Right76.CALL, **HULL)
    put = price(Right76.PUT, **HULL)
    assert call == pytest.approx(put, rel=1e-12)


@pytest.mark.parametrize("strike", [15.0, 18.0, 20.0, 22.5, 30.0])
@pytest.mark.parametrize("rate", [0.0, 0.05])
def test_put_call_parity(strike: float, rate: float) -> None:
    kw = {**HULL, "strike": strike, "rate": rate}
    call = price(Right76.CALL, **kw)
    put = price(Right76.PUT, **kw)
    expected = math.exp(-rate * HULL["years"]) * (HULL["forward"] - strike)
    assert call - put == pytest.approx(expected, abs=1e-12)


def test_known_value_nq_scale() -> None:
    # Independent closed form evaluated by hand for an NQ-sized at-the-money option:
    # F = K = 29600, sigma = 0.24, T = 1/365, r = 0 -> C = F * (2 N(s/2) - 1) with
    # s = sigma * sqrt(T); N(x) = (1 + erf(x / sqrt 2)) / 2.
    s = 0.24 * math.sqrt(1 / 365)
    expected = 29600 * math.erf(s / 2 / math.sqrt(2))
    got = price(Right76.CALL, forward=29600, strike=29600, years=1 / 365, vol=0.24, rate=0.0)
    assert got == pytest.approx(expected, rel=1e-12)
    # About 147 points: the size of the at-the-money premium the recorder saw.
    assert 140 < got < 155


def test_deep_in_the_money_call_is_discounted_intrinsic() -> None:
    got = price(Right76.CALL, forward=200.0, strike=100.0, years=0.1, vol=0.2, rate=0.05)
    assert got == pytest.approx(math.exp(-0.005) * 100.0, rel=1e-9)


def test_deep_out_of_the_money_is_worth_almost_nothing() -> None:
    got = price(Right76.CALL, forward=100.0, strike=200.0, years=0.1, vol=0.2, rate=0.0)
    assert 0.0 <= got < 1e-12


def test_price_rises_with_vol_and_time() -> None:
    base = price(Right76.CALL, **HULL)
    assert price(Right76.CALL, **{**HULL, "vol": 0.3}) > base
    assert price(Right76.CALL, **{**HULL, "years": 0.5}) > base


# --- expiry and zero volatility ---------------------------------------------


@pytest.mark.parametrize(
    ("right", "forward", "expected"),
    [
        (Right76.CALL, 25.0, 5.0),
        (Right76.CALL, 15.0, 0.0),
        (Right76.PUT, 15.0, 5.0),
        (Right76.PUT, 25.0, 0.0),
        (Right76.CALL, 20.0, 0.0),
        (Right76.PUT, 20.0, 0.0),
    ],
)
def test_at_expiry_price_is_intrinsic(right: Right76, forward: float, expected: float) -> None:
    got = greeks(right, forward=forward, strike=20.0, years=0.0, vol=0.25, rate=0.09)
    assert got.price == expected
    assert got.gamma == 0.0
    assert got.vega == 0.0
    assert got.theta == 0.0


@pytest.mark.parametrize(
    ("right", "forward", "delta"),
    [
        (Right76.CALL, 25.0, 1.0),
        (Right76.CALL, 15.0, 0.0),
        (Right76.CALL, 20.0, 0.0),
        (Right76.PUT, 15.0, -1.0),
        (Right76.PUT, 25.0, 0.0),
        (Right76.PUT, 20.0, 0.0),
    ],
)
def test_at_expiry_delta_is_a_step(right: Right76, forward: float, delta: float) -> None:
    got = greeks(right, forward=forward, strike=20.0, years=0.0, vol=0.25, rate=0.0)
    assert got.delta == delta


def test_zero_vol_is_discounted_intrinsic_with_no_gamma() -> None:
    got = greeks(Right76.CALL, forward=25.0, strike=20.0, years=0.5, vol=0.0, rate=0.04)
    disc = math.exp(-0.02)
    assert got.price == pytest.approx(disc * 5.0, rel=1e-15)
    assert got.delta == pytest.approx(disc, rel=1e-15)
    assert got.gamma == 0.0
    assert got.vega == 0.0
    # With no volatility, the only time value is the discount: theta = r * price.
    assert got.theta == pytest.approx(0.04 * disc * 5.0, rel=1e-12)


def test_zero_vol_out_of_the_money_put_is_worth_nothing() -> None:
    got = greeks(Right76.PUT, forward=25.0, strike=20.0, years=0.5, vol=0.0, rate=0.04)
    assert got.price == 0.0
    assert got.delta == 0.0


# --- Greeks agree with finite differences ------------------------------------


CASES = [
    (Right76.CALL, 20.0, 20.0, 4 / 12, 0.25, 0.09),
    (Right76.PUT, 20.0, 20.0, 4 / 12, 0.25, 0.09),
    (Right76.CALL, 29600.0, 29500.0, 2 / 365, 0.24, 0.0),
    (Right76.PUT, 29600.0, 29700.0, 2 / 365, 0.24, 0.0),
    (Right76.CALL, 7733.0, 7800.0, 7 / 365, 0.16, 0.04),
    (Right76.PUT, 7733.0, 7650.0, 7 / 365, 0.16, 0.04),
]


@pytest.mark.parametrize(("right", "f", "k", "t", "v", "r"), CASES)
def test_greeks_match_finite_differences(
    right: Right76, f: float, k: float, t: float, v: float, r: float
) -> None:
    g = greeks(right, forward=f, strike=k, years=t, vol=v, rate=r)

    def p(**over: float) -> float:
        kw = {"forward": f, "strike": k, "years": t, "vol": v, "rate": r, **over}
        return price(right, **kw)

    hf = f * 1e-5
    assert g.price == pytest.approx(p(), rel=1e-15)
    assert g.delta == pytest.approx((p(forward=f + hf) - p(forward=f - hf)) / (2 * hf), abs=1e-6)
    gamma_fd = (p(forward=f + hf) - 2 * p() + p(forward=f - hf)) / (hf * hf)
    assert g.gamma == pytest.approx(gamma_fd, rel=1e-3)
    hv = 1e-6
    assert g.vega == pytest.approx((p(vol=v + hv) - p(vol=v - hv)) / (2 * hv), rel=1e-6)
    ht = t * 1e-6
    # Theta is the value's change as time passes (time to expiry shrinks), per year.
    assert g.theta == pytest.approx(-(p(years=t + ht) - p(years=t - ht)) / (2 * ht), rel=1e-5)


def test_long_option_greek_signs() -> None:
    call = greeks(Right76.CALL, **HULL)
    put = greeks(Right76.PUT, **HULL)
    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma > 0
    assert call.gamma == pytest.approx(put.gamma, rel=1e-12)
    assert call.vega > 0
    assert call.vega == pytest.approx(put.vega, rel=1e-12)
    assert call.theta < 0


def test_call_minus_put_delta_is_the_discount() -> None:
    call = greeks(Right76.CALL, **HULL)
    put = greeks(Right76.PUT, **HULL)
    assert call.delta - put.delta == pytest.approx(math.exp(-0.09 * 4 / 12), rel=1e-12)


def test_short_dated_theta_is_much_bigger_than_long_dated() -> None:
    near = greeks(Right76.CALL, forward=29600, strike=29600, years=1 / 365, vol=0.24, rate=0)
    far = greeks(Right76.CALL, forward=29600, strike=29600, years=30 / 365, vol=0.24, rate=0)

    # At the money with r = 0, theta = -F n(d1) sigma / (2 sqrt T) with d1 = s / 2, so
    # the ratio is sqrt(30) times the ratio of the normal densities at the two d1.
    def n(x: float) -> float:
        return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)

    d_near = 0.24 * math.sqrt(1 / 365) / 2
    d_far = 0.24 * math.sqrt(30 / 365) / 2
    expected = math.sqrt(30) * n(d_near) / n(d_far)
    assert near.theta / far.theta == pytest.approx(expected, rel=1e-12)
    assert near.theta / far.theta > 5


def test_greeks_returns_the_inputs_it_used() -> None:
    g = greeks(Right76.PUT, **HULL)
    assert isinstance(g, Black76)
    assert g.right is Right76.PUT


# --- invalid input fails loudly ----------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"forward": 0.0},
        {"forward": -1.0},
        {"strike": 0.0},
        {"strike": -5.0},
        {"years": -1e-9},
        {"vol": -0.01},
        {"forward": math.nan},
        {"strike": math.inf},
        {"years": math.nan},
        {"vol": math.inf},
        {"rate": math.nan},
    ],
)
@pytest.mark.parametrize("fn", [price, greeks])
def test_invalid_inputs_raise(fn: object, bad: dict[str, float]) -> None:
    assert callable(fn)
    with pytest.raises(ValueError, match="Black-76"):
        fn(Right76.CALL, **{**HULL, **bad})


def test_right_must_be_a_right() -> None:
    with pytest.raises(TypeError, match="Right76"):
        price("C", **HULL)  # type: ignore[arg-type]


def test_negative_rate_is_allowed() -> None:
    got = price(Right76.CALL, **{**HULL, "rate": -0.01})
    assert math.isfinite(got)
    assert got > price(Right76.CALL, **HULL)


def test_bool_is_not_a_number() -> None:
    with pytest.raises(TypeError, match="Black-76"):
        price(Right76.CALL, **{**HULL, "vol": True})


def test_integers_are_accepted() -> None:
    assert price(Right76.CALL, forward=20, strike=20, years=1, vol=1, rate=0) > 0


def test_rounding_never_gives_a_negative_value() -> None:
    # Far out of the money, the two terms of the formula cancel to within float rounding
    # and can come out a hair below zero (about -7.5e-13 here). An option is never worth
    # less than nothing: the value is floored at 0.
    got = price(Right76.PUT, forward=29600.0, strike=8000.0, years=0.1, vol=0.5, rate=0.0)
    assert got >= 0.0
