"""Black-76: the price and Greeks of a European option on a futures contract.

    s  = vol * sqrt(years)
    d1 = (ln(forward) - ln(strike) + half of s squared) / s,   d2 = d1 - s
    D  = exp(-rate * years)                          (discount to today)
    call = D * (forward * N(d1) - strike * N(d2))
    put  = D * (strike * N(-d2) - forward * N(-d1))

Units, so no caller has to guess:
- `years` is time to expiry in years, on whatever clock the caller chooses (calendar or
  trading time); the model assumes variance accrues evenly over it, so `vol` must be
  annualised on the same clock. `vol` is a fraction per year; `rate` is a continuously
  compounded annual rate (may be negative).
- price and delta are in the option's own price points per futures point; gamma per
  futures point squared. With a negative rate, |delta| can exceed 1 slightly (the
  discount factor is above 1): correct, not a bug.
- vega is per whole unit of volatility (IBKR quotes vega per one percent of vol, a
  hundredth of this).
- theta is the INSTANTANEOUS rate of change per YEAR of time passing, signed as the
  change in value as expiry comes nearer (negative for a long option that decays).
  Near expiry it is far larger than any real day's decay (with minutes left it can
  exceed the whole premium per day). To charge decay over a holding period, use the
  model's price difference over that period, not theta times the period, unless the
  period is small next to the time left. The caller owns any day count.

Edge cases, defined rather than left to a division by zero:
- `years == 0` (at expiry): price is the intrinsic value; delta is a step (+1 for a call
  or -1 for a put only when strictly in the money, 0 at the money and out of it);
  gamma, vega and theta are 0.
- `vol * sqrt(years) == 0` with time left (no volatility, or a volatility too small to
  register): price is the discounted intrinsic value; delta is +discount (call) or
  -discount (put) when strictly in the money, else 0; gamma and vega are 0; theta is
  `rate * price` (the only time value left is the discount).
- Inputs whose arithmetic leaves the float range (an overflowing discount, a volatility
  so large its square is infinite, an integer too large for a float) raise ValueError,
  like every other impossible input; nothing returns a silently wrong record.

The math runs in float on purpose: this is a model, evaluated many times per minute in
research, and the quantities it returns are estimates. A caller that books money keeps
its own Decimal arithmetic and never books a modelled price (never book a guessed price).
Nothing here bounds `vol` from above (a percent typed where a fraction belongs prices
nonsense): a sanity bound belongs in the caller's config.

Exercise style (the exchange rulebook, chapter 359A): quarterly Nasdaq-100 futures options are
American (exercisable any time before expiry); the weekly and end-of-month ones are
European. Black-76 values a European option, so for a quarterly expiry it slightly
UNDERPRICES (the early-exercise premium, largest deep in the money with a positive
rate, small for short-dated options) and an implied volatility backed out of a
quarterly's price is biased. Prefer European expiries when using this module.
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
    # erfc keeps full precision in the far left tail, where 1 + erf would cancel.
    return math.erfc(-x / _ROOT_TWO) / 2


def _pdf(x: float) -> float:
    return math.exp(-x * x / 2) / _ROOT_TWO_PI


def _number(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"Black-76: {name} must be a number, got {value!r}")
    try:
        as_float = float(value)
    except OverflowError as exc:
        raise ValueError(f"Black-76: {name} is too large for a float") from exc
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
        outputs = (price, delta, gamma, vega, theta)
        if not all(math.isfinite(x) for x in outputs):
            raise ValueError(f"Black-76: a result is out of float range: {outputs!r}")
        return Black76(right, f, k, t, v, r, price, delta, gamma, vega, theta)

    if t == 0:
        return result(intrinsic, sign if in_the_money else 0.0, 0.0, 0.0, 0.0)
    try:
        discount = math.exp(-r * t)
    except OverflowError as exc:
        raise ValueError(f"Black-76: the discount exp({-r * t!r}) is out of float range") from exc
    if not math.isfinite(discount):
        raise ValueError(f"Black-76: the discount exp({-r * t!r}) is out of float range")

    def no_volatility() -> Black76:
        value = discount * intrinsic
        delta = sign * discount if in_the_money else 0.0
        return result(value, delta, 0.0, 0.0, r * value)

    root_t = math.sqrt(t)
    s = v * root_t
    if s == 0:
        return no_volatility()

    drift = s * s / 2
    if not math.isfinite(drift):
        raise ValueError(f"Black-76: vol {vol!r} over {years!r} years is out of float range")
    # With a volatility so small that d1 overflows to +/- infinity, the normal cdf and
    # density saturate to 1, 0 and 0, which is exactly the zero-volatility answer.
    d1 = (math.log(f) - math.log(k) + drift) / s
    d2 = d1 - s
    if call:
        value = discount * (f * _cdf(d1) - k * _cdf(d2))
        delta = discount * _cdf(d1)
    else:
        value = discount * (k * _cdf(-d2) - f * _cdf(-d1))
        delta = -discount * _cdf(-d1)
    value = max(value, 0.0)
    density = _pdf(d1)
    scale = f * s
    if scale == 0:
        raise ValueError(f"Black-76: forward {forward!r} times vol is below float range")
    gamma = discount * density / scale
    vega = discount * f * density * root_t
    theta = -discount * f * density * v / (2 * root_t) + r * value
    return result(value, delta, gamma, vega, theta)


def price(
    right: Right76, *, forward: float, strike: float, years: float, vol: float, rate: float
) -> float:
    """The model value of one option (see `greeks` for units and edge cases)."""
    return greeks(right, forward=forward, strike=strike, years=years, vol=vol, rate=rate).price
