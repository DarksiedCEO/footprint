"""Black-76: the price and Greeks of a European option on a futures contract.

    s  = vol * sqrt(years)
    d1 = (ln(forward / strike) + half of s squared) / s,   d2 = d1 - s
    D  = exp(-rate * years)                          (discount to today)
    call = D * (forward * N(d1) - strike * N(d2))
    put  = D * (strike * N(-d2) - forward * N(-d1))

Units, so no caller has to guess:
- `years` is time to expiry in years; `vol` is annualised (a fraction per year); `rate` is a
  continuously compounded annual rate (may be negative).
- price and delta are in the option's own price points per futures point; gamma per
  futures point squared.
- vega is per whole unit of volatility (IBKR quotes vega per one percent of vol, a
  hundredth of this).
- theta is per YEAR of time passing, signed as the change in value as expiry comes
  nearer (negative for a long option that decays). A caller wanting it per day divides
  by its own day count; that count is the caller's (and config's) choice, not this
  module's.

Edge cases, defined rather than left to a division by zero:
- `years == 0` (at expiry): price is the intrinsic value; delta is a step (1 / -1 only
  when strictly in the money, 0 at the money and out of it); gamma, vega and theta are 0.
- `vol == 0` (no volatility) with time left: price is the discounted intrinsic value;
  delta is the discount when strictly in the money, else 0; gamma and vega are 0; theta
  is `rate * price` (the only time value left is the discount).

The math runs in float on purpose: this is a model, evaluated many times per minute in
research, and the quantities it returns are estimates. A caller that books money keeps
its own Decimal arithmetic and never books a modelled price (never book a guessed price).

UNVERIFIED for the exchange's index options: whether every listed expiry is European. Early
exercise cannot matter for an option bought and sold on the same day, so Black-76 is
used for those; a holder over expiry would need the contract's own exercise terms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

_ROOT_TWO = math.sqrt(2)
_ROOT_TWO_PI = math.sqrt(math.tau)


class Right76(Enum):
    CALL = "C"
    PUT = "P"


@dataclass(frozen=True, slots=True)
class Black76:
    """One option's model value and Greeks, with the inputs that produced them."""

    right: Right76
    forward: float
    strike: float
    years: float
    vol: float
    rate: float
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float


def _cdf(x: float) -> float:
    return (1 + math.erf(x / _ROOT_TWO)) / 2


def _pdf(x: float) -> float:
    return math.exp(-x * x / 2) / _ROOT_TWO_PI


def _number(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"Black-76: {name} must be a number, got {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float):
        raise ValueError(f"Black-76: {name} must be finite, got {value!r}")
    return as_float


def _checked(
    right: Right76, forward: float, strike: float, years: float, vol: float, rate: float
) -> tuple[float, float, float, float, float]:
    if not isinstance(right, Right76):
        raise TypeError(f"Black-76: right must be a Right76, got {right!r}")
    f = _number("forward", forward)
    k = _number("strike", strike)
    t = _number("years", years)
    v = _number("vol", vol)
    r = _number("rate", rate)
    if f <= 0:
        raise ValueError(f"Black-76: forward must be > 0, got {forward!r}")
    if k <= 0:
        raise ValueError(f"Black-76: strike must be > 0, got {strike!r}")
    if t < 0:
        raise ValueError(f"Black-76: years must be >= 0, got {years!r}")
    if v < 0:
        raise ValueError(f"Black-76: vol must be >= 0, got {vol!r}")
    return f, k, t, v, r


def greeks(
    right: Right76, *, forward: float, strike: float, years: float, vol: float, rate: float
) -> Black76:
    """The model value and Greeks of one option. Raises on any impossible input."""
    f, k, t, v, r = _checked(right, forward, strike, years, vol, rate)
    call = right is Right76.CALL
    intrinsic = max(f - k, 0.0) if call else max(k - f, 0.0)
    in_the_money = f > k if call else f < k
    sign = 1.0 if call else -1.0

    def result(price: float, delta: float, gamma: float, vega: float, theta: float) -> Black76:
        return Black76(right, f, k, t, v, r, price, delta, gamma, vega, theta)

    if t == 0:
        return result(intrinsic, sign if in_the_money else 0.0, 0.0, 0.0, 0.0)
    discount = math.exp(-r * t)
    if v == 0:
        value = discount * intrinsic
        delta = sign * discount if in_the_money else 0.0
        return result(value, delta, 0.0, 0.0, r * value)

    root_t = math.sqrt(t)
    s = v * root_t
    d1 = (math.log(f / k) + s * s / 2) / s
    d2 = d1 - s
    if call:
        value = discount * (f * _cdf(d1) - k * _cdf(d2))
        delta = discount * _cdf(d1)
    else:
        value = discount * (k * _cdf(-d2) - f * _cdf(-d1))
        delta = -discount * _cdf(-d1)
    density = _pdf(d1)
    gamma = discount * density / (f * s)
    vega = discount * f * density * root_t
    theta = -discount * f * density * v / (2 * root_t) + r * value
    return result(max(value, 0.0), delta, gamma, vega, theta)


def price(
    right: Right76, *, forward: float, strike: float, years: float, vol: float, rate: float
) -> float:
    """The model value of one option (see `greeks` for units and edge cases)."""
    return greeks(right, forward=forward, strike=strike, years=years, vol=vol, rate=rate).price
