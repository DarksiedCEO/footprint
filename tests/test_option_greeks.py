"""OptionGreeks: one option's model Greeks (IBKR's tick type 13), self-validating.

A reading that is impossible for an option (a negative gamma, a call delta above 1,
a volatility of 0, ...) can never be built; `checked_greeks` turns such a reading, or
one with nothing computed at all, into None, so a caller can only ever hold a reading
that could be real. Theta is the one Greek with no sign rule: a deep in-the-money
European option can have positive theta."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from footprint.core.options import OptionGreeks, Right, checked_greeks

D = Decimal
T = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
GOOD: dict[str, Any] = {
    "implied_vol": D("0.1834"),
    "delta": D("0.512"),
    "gamma": D("0.0009"),
    "vega": D("6.1"),
    "theta": D("-11.4"),
    "underlying_price": D("25012.25"),
}


def put_values(**changes: Any) -> dict[str, Any]:
    return {**GOOD, "delta": D("-0.488"), **changes}


def test_a_real_reading_is_kept_exactly() -> None:
    g = checked_greeks(Right.CALL, T, **GOOD)
    assert g == OptionGreeks(Right.CALL, **GOOD, ts=T)


def test_a_put_reading_with_a_negative_delta_is_kept() -> None:
    g = checked_greeks(Right.PUT, T, **put_values())
    assert g is not None
    assert g.delta == D("-0.488")


def test_fields_ibkr_did_not_compute_stay_none() -> None:
    g = checked_greeks(
        Right.CALL,
        T,
        implied_vol=D("0.2"),
        delta=None,
        gamma=None,
        vega=None,
        theta=None,
        underlying_price=None,
    )
    assert g is not None
    assert (g.implied_vol, g.delta, g.gamma, g.vega, g.theta, g.underlying_price) == (
        D("0.2"),
        None,
        None,
        None,
        None,
        None,
    )


def test_a_reading_with_nothing_computed_is_none() -> None:
    nothing = dict.fromkeys(GOOD)
    assert checked_greeks(Right.CALL, T, **nothing) is None


@pytest.mark.parametrize(
    ("right", "changes"),
    [
        (Right.CALL, {"implied_vol": D(0)}),
        (Right.CALL, {"implied_vol": D("-0.01")}),
        (Right.CALL, {"delta": D("1.0001")}),
        (Right.CALL, {"delta": D("-0.0001")}),
        (Right.PUT, {"delta": D("0.0001")}),
        (Right.PUT, {"delta": D("-1.0001")}),
        (Right.CALL, {"gamma": D("-0.0001")}),
        (Right.CALL, {"vega": D("-0.01")}),
        (Right.CALL, {"underlying_price": D(0)}),
        (Right.CALL, {"underlying_price": D(-5)}),
        (Right.CALL, {"theta": D("NaN")}),
        (Right.CALL, {"vega": D("Infinity")}),
    ],
)
def test_an_impossible_reading_is_none_as_a_whole(right: Right, changes: dict[str, Any]) -> None:
    values = put_values(**changes) if right is Right.PUT else {**GOOD, **changes}
    assert checked_greeks(right, T, **values) is None
    with pytest.raises(ValueError, match="OptionGreeks"):
        OptionGreeks(right, **values, ts=T)


@pytest.mark.parametrize(
    ("right", "delta"),
    [(Right.CALL, D(0)), (Right.CALL, D(1)), (Right.PUT, D(0)), (Right.PUT, D(-1))],
)
def test_delta_bounds_are_inclusive(right: Right, delta: Decimal) -> None:
    assert checked_greeks(right, T, **{**GOOD, "delta": delta}) is not None


def test_zero_gamma_and_zero_vega_are_possible() -> None:
    assert checked_greeks(Right.CALL, T, **{**GOOD, "gamma": D(0), "vega": D(0)}) is not None


def test_theta_has_no_sign_rule() -> None:
    assert checked_greeks(Right.CALL, T, **{**GOOD, "theta": D("0.3")}) is not None
    assert checked_greeks(Right.CALL, T, **{**GOOD, "theta": D(0)}) is not None


def test_a_tiny_positive_volatility_is_possible() -> None:
    assert checked_greeks(Right.CALL, T, **{**GOOD, "implied_vol": D("0.0001")}) is not None


def test_the_time_must_be_utc() -> None:
    with pytest.raises(ValueError, match="ts"):
        OptionGreeks(Right.CALL, **GOOD, ts=T.replace(tzinfo=None))
    pacific = timezone(timedelta(hours=-7))
    with pytest.raises(ValueError, match="ts"):
        OptionGreeks(Right.CALL, **GOOD, ts=T.astimezone(pacific))


def test_values_must_be_decimals() -> None:
    with pytest.raises(TypeError, match="delta"):
        OptionGreeks(Right.CALL, **{**GOOD, "delta": 0.5}, ts=T)
    with pytest.raises(TypeError, match="right"):
        OptionGreeks("CALL", **GOOD, ts=T)  # type: ignore[arg-type]


def test_a_wrong_type_is_not_swallowed_by_checked_greeks() -> None:
    """Only an impossible VALUE is turned into None; a caller passing floats is a bug."""
    with pytest.raises(TypeError):
        checked_greeks(Right.CALL, T, **{**GOOD, "gamma": 0.001})


def test_a_bad_time_is_a_callers_bug_not_a_none() -> None:
    with pytest.raises(ValueError, match="ts"):
        checked_greeks(Right.CALL, T.replace(tzinfo=None), **GOOD)
    with pytest.raises(ValueError, match="ts"):
        checked_greeks(Right.CALL, T.replace(tzinfo=None), **dict.fromkeys(GOOD))
