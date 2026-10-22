"""Implied volatility: the Black-76 volatility that reproduces an option's price.

Solved by bisection between the caller's `low` and `high` volatility bounds. A Black-76
price rises strictly with volatility while time is left, so a price between the prices
at the two bounds has exactly one answer, and bisection finds it to the last float: the
loop stops when the bracket's midpoint can no longer be told apart from its ends. No
iteration count and no tolerance are chosen by hand.

A price outside the bounds' prices raises ImpliedVolOutOfRange (a ValueError) naming the
side and the bound: below the low bound (this includes any price under the discounted
intrinsic value, which no volatility can produce) or above the high one. Every other
impossible input raises a plain ValueError or TypeError, as Black-76 does: a price that
is not a finite number above 0, no time left (a price at expiry says nothing about
volatility), or bounds that are not finite, positive and in order.

Where the price barely moves with volatility (deep in the money with almost no time
left), many volatilities give the same price to the last float; the answer is then one
of them, which is harmless for a decay estimate (the time value is near nothing).

Units are Black-76's (`years` on the caller's clock, `vol` per year on the same clock).
The answer is a model estimate: never book a price from it.
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Literal

from footprint.core import black76
from footprint.core.black76 import Right76


class ImpliedVolOutOfRange(ValueError):
    """The price needs a volatility outside [low, high]."""

    def __init__(self, side: Literal["below", "above"], bound: float, quoted: float) -> None:
        self.side = side
        self.bound = bound
        super().__init__(
            f"implied volatility is {side} the bound {bound!r}: no volatility in range "
            f"prices this option at {quoted!r}"
        )


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"implied volatility: {name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"implied volatility: {name} must be finite, got {value!r}")
    return number


def implied_vol(
    right: Right76,
    *,
    forward: float,
    strike: float,
    years: float,
    rate: float,
    price: float,
    low: float,
    high: float,
) -> float:
    """The volatility in [low, high] at which Black-76 gives `price`."""
    quoted = _finite("price", price)
    lo = _finite("low", low)
    hi = _finite("high", high)
    t = _finite("years", years)
    if quoted <= 0:
        raise ValueError(f"implied volatility: price must be > 0, got {price!r}")
    if lo <= 0 or hi <= lo:
        raise ValueError(
            f"implied volatility: bounds must satisfy 0 < low < high, got {low!r}, {high!r}"
        )
    if t <= 0:
        raise ValueError(f"implied volatility: no time left ({years!r} years) to imply it from")

    def value(vol: float) -> float:
        return _model(right, forward, strike, t, vol, rate)

    if quoted < value(lo):
        raise ImpliedVolOutOfRange("below", lo, quoted)
    if quoted > value(hi):
        raise ImpliedVolOutOfRange("above", hi, quoted)
    while True:
        middle = fmean((lo, hi))
        if middle <= lo or middle >= hi:
            return middle
        if value(middle) < quoted:
            lo = middle
        else:
            hi = middle


def _model(
    right: Right76, forward: float, strike: float, years: float, vol: float, rate: float
) -> float:
    return black76.price(right, forward=forward, strike=strike, years=years, vol=vol, rate=rate)
