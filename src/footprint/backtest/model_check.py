"""Refuse a backtest the option model cannot trade (greeks-veto-v1 review round 1, H1).

The model (options_model.py, G4) prices the at-the-money option at a fixed
`atm_premium_points` x root(days to expiry), whatever the futures price. The implied
volatility of its quotes therefore moves like 1 / price: far enough below the prices it
was set for, Theta and Vega find it too high and veto every entry; far enough above,
Vega finds it too low and vetoes every entry. A run over such prices would end with no
trades and no error. How to price the model instead is Andre's decision GV-4.

Until then this check runs before a backtest starts. At the lowest and at the highest
price traded in the prime session (the only session with entries), it values the
contracts the model would offer Felix exactly as the vetoes do (B76-1 calendar time to
the expiry moment, theta.rate, the Black-76 implied volatility of the mid) and refuses
to run, naming that price, when the volatility is outside theta's bounds or the vega
band. Checked per moment: the at-the-money call and put of the nearest listed expiry
at least dte_min days from the trading day (Felix's expiry; when none is inside
dte_max, still the nearest, since the model's volatility hardly depends on the days
left). It is a guard on the model, not a forecast of any trade: single entries can
still be vetoed, and are journalled. It has no margin at the edges of the band: near
an edge the verdict moves with the time of day and the strike offset, so a run whose
extreme sits there can pass while some entries near that extreme are vetoed (GV-3).
A run with no traded prime-session bar is refused too: it could make no entry.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from footprint.backtest.options_model import OptionModel
from footprint.core.black76 import Right76
from footprint.core.calendar import SessionKind, session_at, trading_day_at
from footprint.core.config import Profiles
from footprint.core.expiry import expiry_moment
from footprint.core.implied_vol import ImpliedVolOutOfRange, implied_vol
from footprint.core.models import Bar
from footprint.core.options import OptionQuote, Right

_YEAR = timedelta(days=365)  # B76-1: calendar years, as the vetoes count them


class BacktestRefused(ValueError):
    """The backtest will not run: it could only trade nothing, silently."""


class ModelOutsideBand(BacktestRefused):
    """At a price the backtest trades, the option model's implied volatility is outside
    the band the vetoes accept: the run would trade nothing there, silently."""


class NoPrimeSession(BacktestRefused):
    """No traded bar is in the prime session, the only session with entries."""


def model_problem(
    model: OptionModel, profiles: Profiles, moment: datetime, price: Decimal
) -> str | None:
    """Why the vetoes would refuse every contract the model offers at `moment` and
    `price`, or None when the at-the-money call and put both value inside the band."""
    inst = profiles.instrument
    today = trading_day_at(moment, inst)
    if today is None:
        return None  # the market is closed: nothing is traded at this moment
    chain = model.chain(moment, price)
    ahead = sorted({q.expiry for q in chain.quotes if (q.expiry - today).days >= inst.dte_min})
    if not ahead:
        return None
    expiry = ahead[0]
    for right in Right:
        quotes = [q for q in chain.quotes if q.expiry == expiry and q.right is right]
        quote = min(quotes, key=lambda q: _nearest(q, price, right))
        problem = _value(quote, profiles, moment, price)
        if problem is not None:
            return problem
    return None


def check_model_vol(model: OptionModel, profiles: Profiles, traded: Sequence[Bar]) -> None:
    """Raise ModelOutsideBand when the model fails at the lowest or the highest price of
    the traded bars inside the prime session (on a tie, the earliest such bar), and
    NoPrimeSession when there is no traded prime bar at all (second review, R2-1:
    fail closed, such a run could make no entry)."""
    prime = [b for b in traded if session_at(b.ts, profiles.instrument) is SessionKind.PRIME]
    if not prime:
        raise NoPrimeSession(
            "refused: no traded bar is in the prime session, the only session in which "
            "entries are made: the run could only trade nothing"
        )
    low = min(prime, key=lambda b: (b.low, b.ts))
    high = max(prime, key=lambda b: (b.high, -b.ts.timestamp()))
    for bar, price, which in ((low, low.low, "lowest"), (high, high.high, "highest")):
        problem = model_problem(model, profiles, bar.ts, price)
        if problem is not None:
            symbol = profiles.instrument.symbol
            raise ModelOutsideBand(
                f"refused: the backtest's option model cannot be traded at {symbol} {price}, "
                f"the {which} price traded in the prime session (bar {bar.ts.isoformat()}): "
                f"{problem}. The model prices the at-the-money option at a fixed "
                "atm_premium_points x root(days to expiry) whatever the price, so its "
                "implied volatility rises as the price falls; a run here would trade "
                "nothing near this price, without an error. How to price the model is "
                "Andre's decision GV-4 (docs/decisions-pending.md)"
            )


def _nearest(q: OptionQuote, price: Decimal, right: Right) -> tuple[Decimal, Decimal]:
    """Felix's strike: the nearest to the price; a tie goes to the in-the-money side."""
    return abs(q.strike - price), q.strike if right is Right.CALL else -q.strike


def _value(quote: OptionQuote, profiles: Profiles, moment: datetime, price: Decimal) -> str | None:
    s = profiles.strategy
    years = (expiry_moment(quote.expiry, profiles.instrument) - moment) / _YEAR
    if years <= 0:
        return None  # expired: Felix's dte_min decides that, not this check
    name = f"the model's {quote.right.value.lower()} {quote.strike} expiring {quote.expiry}"
    try:
        vol = implied_vol(
            Right76.CALL if quote.right is Right.CALL else Right76.PUT,
            forward=float(price),
            strike=float(quote.strike),
            years=years,
            rate=float(s.theta.rate),
            price=float(quote.mid),
            low=float(s.theta.min_implied_vol),
            high=float(s.theta.max_implied_vol),
        )
    except ImpliedVolOutOfRange as exc:
        key = "min_implied_vol" if exc.side == "below" else "max_implied_vol"
        bound = getattr(s.theta, key)
        return (
            f"{name} (mid {quote.mid}) has an implied volatility {exc.side} "
            f"theta.{key} {bound}: theta would veto every entry here"
        )
    for side, key, outside in (
        ("below", "min_implied_vol", vol < float(s.vega.min_implied_vol)),
        ("above", "max_implied_vol", vol > float(s.vega.max_implied_vol)),
    ):
        if outside:
            bound = getattr(s.vega, key)
            return (
                f"{name} (mid {quote.mid}) has an implied volatility {vol:.4f}, {side} "
                f"vega.{key} {bound}: vega would veto every entry here"
            )
    return None
