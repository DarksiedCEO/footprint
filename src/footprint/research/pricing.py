"""Values a bought option, minute by minute, with Black-76 (core/black76.py).

The option is chosen at entry from config (config/research.yaml `option`):
- direction +1 buys a call, -1 buys a put. Nothing is ever sold short: the harness only
  ever buys an option and later sells that same option.
- strike = entry futures price - direction * moneyness_points (positive moneyness = in
  the money for the trade's direction), rounded to the nearest `strike_step`
  (backtest.yaml option_model.strike_step; a tie goes to the even multiple).
- years to expiry = (days_to_expiry - calendar days elapsed since entry) / days_per_year.
  An option valued after its expiry is an error, never a guess.
- price-level scaling: with `reference_price` set, every forward and strike of the trade
  is multiplied by reference_price / entry futures price. Black-76 is homogeneous of
  degree one in forward and strike, so this rescales the option exactly (a test pins it)
  and fixed dollar amounts (costs, stops) mean the same in every year of the data.

UNVERIFIED: the volatility used (a daily index level, research/vol.py) and the rate.
Black-76 theta is an instantaneous per-year rate; decay over a holding period is taken
from the price difference (the harness reprices every minute), never from theta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from footprint.core.black76 import Right76, price
from footprint.research.settings import OptionSettings

_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Choice:
    """The option bought: its right, strike (in the data's own price level), and the
    factor that rescales forward and strike to the reference price level."""

    right: Right76
    direction: int
    strike: float
    scale: float
    expiry: datetime  # entry time + days_to_expiry calendar days


def choose(direction: int, entry_price: float, entry_ts: datetime, opt: OptionSettings) -> Choice:
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 (call) or -1 (put), got {direction!r}")
    target = entry_price - direction * opt.moneyness_points
    strike = round(target / opt.strike_step) * opt.strike_step
    scale = 1.0 if opt.reference_price is None else opt.reference_price / entry_price
    right = Right76.CALL if direction == 1 else Right76.PUT
    expiry = entry_ts + opt.days_to_expiry * _DAY
    return Choice(right, direction, strike, scale, expiry)


def years_left(choice: Choice, at: datetime, opt: OptionSettings) -> float:
    days = (choice.expiry - at) / _DAY
    if days < 0:
        raise ValueError(f"option valued after its expiry ({at} is {-days} days past it)")
    return days / opt.days_per_year


def value(choice: Choice, futures: float, at: datetime, vol: float, opt: OptionSettings) -> float:
    """The option's model value (option points, at the reference price level) when the
    future trades at `futures` at time `at`."""
    return price(
        choice.right,
        forward=futures * choice.scale,
        strike=choice.strike * choice.scale,
        years=years_left(choice, at, opt),
        vol=vol,
        rate=opt.rate,
    )


def dollars(
    entry_value: float, exit_value: float, contracts: int, point_value: float, cost: float
) -> float:
    """Net dollars: (exit - entry) * point value * contracts - round trip cost * contracts."""
    return (exit_value - entry_value) * point_value * contracts - cost * contracts
