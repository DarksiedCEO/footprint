"""OptionGreeks: one option's complete model Greeks (IBKR's model computation),
self-validating; `judge_greeks` says what a raw reading is.

Only a usable reading becomes OptionGreeks: complete (every value present) and possible
for an option. A reading with nothing computed is NOT_COMPUTED; with some values missing,
INCOMPLETE (never usable: an agent must never find a None where a Greek should be); with
a value impossible for an option (a negative gamma, a call delta above 1, a volatility
of 0, ...), IMPOSSIBLE. Theta is the one Greek with no sign rule: a deep in-the-money
European option can have positive theta. A caller's wrong type or non-UTC time raises."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from footprint.core.options import (
    GreeksVerdict,
    OptionGreeks,
    Right,
    checked_greeks,
    judge_greeks,
)

D = Decimal
T = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
FIELDS = ("implied_vol", "delta", "gamma", "vega", "theta", "underlying_price")
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


def test_a_real_reading_is_usable_and_kept_exactly() -> None:
    verdict, g = judge_greeks(Right.CALL, T, **GOOD)
    assert verdict is GreeksVerdict.USABLE
    assert g == OptionGreeks(Right.CALL, **GOOD, ts=T)
    assert checked_greeks(Right.CALL, T, **GOOD) == g


def test_a_put_reading_with_a_negative_delta_is_usable() -> None:
    verdict, g = judge_greeks(Right.PUT, T, **put_values())
    assert verdict is GreeksVerdict.USABLE
    assert g is not None
    assert g.delta == D("-0.488")


@pytest.mark.parametrize("missing", FIELDS)
def test_one_value_missing_makes_the_whole_reading_incomplete(missing: str) -> None:
    values = {**GOOD, missing: None}
    assert judge_greeks(Right.CALL, T, **values) == (GreeksVerdict.INCOMPLETE, None)
    assert checked_greeks(Right.CALL, T, **values) is None


@pytest.mark.parametrize("present", FIELDS)
def test_one_value_alone_is_incomplete(present: str) -> None:
    values: dict[str, Any] = {**dict.fromkeys(FIELDS), present: GOOD[present]}
    assert judge_greeks(Right.CALL, T, **values) == (GreeksVerdict.INCOMPLETE, None)


def test_a_reading_with_nothing_computed_is_not_computed() -> None:
    nothing = dict.fromkeys(FIELDS)
    assert judge_greeks(Right.CALL, T, **nothing) == (GreeksVerdict.NOT_COMPUTED, None)
    assert checked_greeks(Right.CALL, T, **nothing) is None


def test_an_incomplete_reading_is_never_judged_impossible() -> None:
    """Missing values are checked first: an impossible delta beside a missing gamma is
    INCOMPLETE (not usable either way)."""
    values = {**GOOD, "delta": D(2), "gamma": None}
    assert judge_greeks(Right.CALL, T, **values) == (GreeksVerdict.INCOMPLETE, None)


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
def test_an_impossible_reading_is_impossible_as_a_whole(
    right: Right, changes: dict[str, Any]
) -> None:
    values = put_values(**changes) if right is Right.PUT else {**GOOD, **changes}
    assert judge_greeks(right, T, **values) == (GreeksVerdict.IMPOSSIBLE, None)
    assert checked_greeks(right, T, **values) is None
    with pytest.raises(ValueError, match="OptionGreeks"):
        OptionGreeks(right, **values, ts=T)


@pytest.mark.parametrize(
    ("right", "delta"),
    [(Right.CALL, D(0)), (Right.CALL, D(1)), (Right.PUT, D(0)), (Right.PUT, D(-1))],
)
def test_delta_bounds_are_inclusive(right: Right, delta: Decimal) -> None:
    assert judge_greeks(right, T, **{**GOOD, "delta": delta})[0] is GreeksVerdict.USABLE


def test_zero_gamma_vega_and_theta_are_possible() -> None:
    values = {**GOOD, "gamma": D(0), "vega": D(0), "theta": D(0)}
    assert judge_greeks(Right.CALL, T, **values)[0] is GreeksVerdict.USABLE


def test_theta_has_no_sign_rule() -> None:
    assert judge_greeks(Right.CALL, T, **{**GOOD, "theta": D("0.3")})[0] is GreeksVerdict.USABLE


def test_a_tiny_positive_volatility_is_possible() -> None:
    values = {**GOOD, "implied_vol": D("0.0001")}
    assert judge_greeks(Right.CALL, T, **values)[0] is GreeksVerdict.USABLE


def test_the_time_must_be_utc() -> None:
    with pytest.raises(ValueError, match="ts"):
        OptionGreeks(Right.CALL, **GOOD, ts=T.replace(tzinfo=None))
    pacific = timezone(timedelta(hours=-7))
    with pytest.raises(ValueError, match="ts"):
        OptionGreeks(Right.CALL, **GOOD, ts=T.astimezone(pacific))


def test_every_value_must_be_a_decimal() -> None:
    with pytest.raises(TypeError, match="delta"):
        OptionGreeks(Right.CALL, **{**GOOD, "delta": 0.5}, ts=T)
    with pytest.raises(TypeError, match="gamma"):
        OptionGreeks(Right.CALL, **{**GOOD, "gamma": None}, ts=T)
    with pytest.raises(TypeError, match="right"):
        OptionGreeks("CALL", **GOOD, ts=T)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values", [{**GOOD, "gamma": 0.001}, {**dict.fromkeys(FIELDS), "delta": 0.5}]
)
def test_a_wrong_type_is_a_callers_bug_never_a_verdict(values: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        judge_greeks(Right.CALL, T, **values)
    with pytest.raises(TypeError):
        checked_greeks(Right.CALL, T, **values)


def test_a_bad_time_is_a_callers_bug_never_a_verdict() -> None:
    for values in (GOOD, dict.fromkeys(FIELDS)):
        with pytest.raises(ValueError, match="ts"):
            judge_greeks(Right.CALL, T.replace(tzinfo=None), **values)
        with pytest.raises(ValueError, match="ts"):
            checked_greeks(Right.CALL, T.replace(tzinfo=None), **values)


def test_a_wrong_right_is_a_callers_bug_never_a_verdict() -> None:
    with pytest.raises(TypeError, match="right"):
        judge_greeks("CALL", T, **GOOD)  # type: ignore[arg-type]
