"""Implied volatility: the Black-76 volatility that reproduces an option's price, solved
inside a caller's bounds (core/implied_vol.py). Every expected value comes from the
Black-76 module itself (certified in PR 36): solving must invert it."""

import math

import pytest

from footprint.core.black76 import Right76, price
from footprint.core.implied_vol import ImpliedVolOutOfRange, implied_vol

LOW, HIGH = 0.05, 1.5

GRID = [
    # right, forward, strike, years, vol, rate
    (Right76.CALL, 20000.0, 20000.0, 1 / 365, 0.25, 0.04),
    (Right76.PUT, 20000.0, 20000.0, 1 / 365, 0.25, 0.04),
    (Right76.CALL, 29600.0, 29400.0, 3 / 365, 0.18, 0.0),
    (Right76.PUT, 29600.0, 29800.0, 3 / 365, 0.31, 0.05),
    (Right76.CALL, 29600.0, 30000.0, 0.5 / 365, 0.7, 0.04),
    (Right76.PUT, 7733.0, 7600.0, 7 / 365, 0.12, -0.01),
    (Right76.CALL, 20000.0, 20500.0, 30 / 365, 1.2, 0.04),
    (Right76.PUT, 20000.0, 19000.0, 1 / 8760, 0.4, 0.04),  # one hour left
]


@pytest.mark.parametrize(("right", "forward", "strike", "years", "vol", "rate"), GRID)
def test_solving_inverts_black76(
    right: Right76, forward: float, strike: float, years: float, vol: float, rate: float
) -> None:
    value = price(right, forward=forward, strike=strike, years=years, vol=vol, rate=rate)
    solved = implied_vol(
        right,
        forward=forward,
        strike=strike,
        years=years,
        rate=rate,
        price=value,
        low=LOW,
        high=HIGH,
    )
    assert solved == pytest.approx(vol, rel=1e-9)
    again = price(right, forward=forward, strike=strike, years=years, vol=solved, rate=rate)
    assert again == pytest.approx(value, rel=1e-12, abs=1e-12)


def test_the_bounds_themselves_are_solvable() -> None:
    args = {"forward": 20000.0, "strike": 20000.0, "years": 2 / 365, "rate": 0.04}
    for vol in (LOW, HIGH):
        value = price(Right76.CALL, vol=vol, **args)
        solved = implied_vol(Right76.CALL, price=value, low=LOW, high=HIGH, **args)
        assert solved == pytest.approx(vol, rel=1e-12)


def test_a_price_below_the_lowest_volatility_is_out_of_range_low() -> None:
    args = {"forward": 20000.0, "strike": 20000.0, "years": 2 / 365, "rate": 0.04}
    floor = price(Right76.CALL, vol=LOW, **args)
    with pytest.raises(ImpliedVolOutOfRange) as caught:
        implied_vol(Right76.CALL, price=floor * 0.999, low=LOW, high=HIGH, **args)
    assert caught.value.side == "below"
    assert caught.value.bound == LOW
    assert isinstance(caught.value, ValueError)


def test_a_price_above_the_highest_volatility_is_out_of_range_high() -> None:
    args = {"forward": 20000.0, "strike": 20000.0, "years": 2 / 365, "rate": 0.04}
    ceiling = price(Right76.PUT, vol=HIGH, **args)
    with pytest.raises(ImpliedVolOutOfRange) as caught:
        implied_vol(Right76.PUT, price=ceiling * 1.001, low=LOW, high=HIGH, **args)
    assert caught.value.side == "above"
    assert caught.value.bound == HIGH


def test_a_price_below_intrinsic_value_is_out_of_range_low() -> None:
    # A deep in-the-money call quoted under its discounted intrinsic value: no
    # volatility can produce it.
    args = {"forward": 20000.0, "strike": 19000.0, "years": 2 / 365, "rate": 0.04}
    with pytest.raises(ImpliedVolOutOfRange) as caught:
        implied_vol(Right76.CALL, price=990.0, low=LOW, high=HIGH, **args)
    assert caught.value.side == "below"


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 0.0},
        {"price": -1.0},
        {"price": math.nan},
        {"price": math.inf},
        {"years": 0.0},  # no time left: the price says nothing about volatility
        {"low": 0.0},
        {"low": -0.1},
        {"low": 1.5},  # low == high
        {"low": 2.0},  # low > high
        {"high": math.inf},
        {"low": math.nan},
    ],
)
def test_impossible_inputs_are_refused(changes: dict[str, float]) -> None:
    args: dict[str, float] = {
        "forward": 20000.0,
        "strike": 20000.0,
        "years": 2 / 365,
        "rate": 0.04,
        "price": 150.0,
        "low": LOW,
        "high": HIGH,
    } | changes
    with pytest.raises(ValueError, match="implied volatility") as caught:
        implied_vol(Right76.CALL, **args)
    assert not isinstance(caught.value, ImpliedVolOutOfRange)


@pytest.mark.parametrize("bad", [True, "150", None])
def test_a_price_that_is_not_a_number_is_refused(bad: object) -> None:
    with pytest.raises(TypeError):
        implied_vol(
            Right76.CALL,
            forward=20000.0,
            strike=20000.0,
            years=2 / 365,
            rate=0.04,
            price=bad,  # type: ignore[arg-type]
            low=LOW,
            high=HIGH,
        )


def test_black76_input_errors_pass_through() -> None:
    with pytest.raises(ValueError, match="Black-76: forward"):
        implied_vol(
            Right76.CALL,
            forward=-1.0,
            strike=20000.0,
            years=2 / 365,
            rate=0.04,
            price=150.0,
            low=LOW,
            high=HIGH,
        )


def test_the_answer_is_the_same_for_the_same_input() -> None:
    args = {"forward": 29600.0, "strike": 29625.0, "years": 1.3 / 365, "rate": 0.04}
    first = implied_vol(Right76.CALL, price=180.0, low=LOW, high=HIGH, **args)
    assert implied_vol(Right76.CALL, price=180.0, low=LOW, high=HIGH, **args) == first
    # higher price, higher volatility
    assert implied_vol(Right76.CALL, price=181.0, low=LOW, high=HIGH, **args) > first
