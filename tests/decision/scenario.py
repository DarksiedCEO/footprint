"""A synthetic but internally consistent market for end-to-end decision tests.

Every trading day repeats the same shape, so each agent's "typical" values are
stable: a 75-minute cycle (trough at minute 7, peak at minute 37) that rises 30
points at 1/min and falls 22.5 at 0.5/min, so the market climbs 7.5 points per
cycle. Fifteen-minute bars therefore make rising swing highs and lows. Each
trading day has its prime session (04:00-13:59) and the overnight session that
led into it (15:00 the previous calendar day to 03:59). Volume is 100 a minute.

At 06:59 on the last day a bullish 3x volume spike prints, followed at 07:00 by a
bar that closes above it: Darius's trigger. At 07:01 everything lines up for a long.
`mirror=True` turns the market upside down (a downtrend) while keeping the same
bullish spike, which must then be blocked as countertrend.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from footprint.core.models import Bar
from footprint.core.options import OptionChain, OptionQuote, Right

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
BASE = D(20000)
DAY_STEP = D(147)  # value(1440) - value(0) of the cycle below
SPIKE_MINUTE = 179  # 06:59 is minute 179 after 04:00
LAST_DAY = date(2026, 9, 15)  # a Tuesday
DAYS = 26


def cycle_value(x: int) -> Decimal:
    """Price offset at minute x after 04:00, continuous across cycles."""
    c, r = divmod(x - 7, 75)
    trough = D("7.5") * c
    return trough + r if r <= 30 else trough + 30 - D("0.5") * (r - 30)


def trading_days(last: date = LAST_DAY, count: int = DAYS) -> list[date]:
    days: list[date] = []
    d = last
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def at(day: date, minute_after_4: int) -> datetime:
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=PT)
    return (start + timedelta(minutes=minute_after_4)).astimezone(UTC)


def _bar(ts: datetime, o: Decimal, c: Decimal, volume: int = 100) -> Bar:
    return Bar("NQ", ts, o, max(o, c) + 1, min(o, c) - 1, c, volume)


def market(
    *,
    mirror: bool = False,
    spike: bool = True,
    spike_minute: int = SPIKE_MINUTE,
    spike_volume: int = 300,
    bearish_spike: bool = False,
    wall_above: Decimal | None = None,
) -> list[Bar]:
    """The synthetic market up to the follow-through bar after the spike.

    `wall_above` plants a high that many points above the final close in the
    previous day's 10:00 bar: a prior-day resistance level.
    """
    days = trading_days()
    sign = D(-1) if mirror else D(1)
    push = D(-3) if bearish_spike else D(3)
    follow = D(-1) if bearish_spike else D(1)
    bars: list[Bar] = []
    for i, day in enumerate(days):
        base = BASE + DAY_STEP * i * sign
        last = i == len(days) - 1
        # The overnight session that led into `day`, then its prime session.
        minutes = [*range(660 - 1440, 0), *range(600 if not last else spike_minute + 2)]
        for x in minutes:
            o = base + sign * cycle_value(x)
            c = base + sign * cycle_value(x + 1)
            ts = at(day, x)
            if last and spike and x == spike_minute:
                c2 = o + push
                bars.append(Bar("NQ", ts, o, max(o, c2) + 1, min(o, c2) - 1, c2, spike_volume))
                continue
            if last and spike and x == spike_minute + 1:
                prev_close = bars[-1].close
                bars.append(_bar(ts, prev_close, prev_close + follow))
                continue
            bars.append(_bar(ts, o, c))
    if wall_above is not None:
        target = at(days[-2], 360)
        level = bars[-1].close + wall_above
        bars = [
            Bar(b.symbol, b.ts, b.open, level, b.low, b.close, b.volume) if b.ts == target else b
            for b in bars
        ]
    return bars


def decision_time(spike_minute: int = SPIKE_MINUTE) -> datetime:
    """Just after the follow-through bar closes."""
    return at(LAST_DAY, spike_minute + 2)


DECISION_TIME = decision_time()  # 07:01 PT


def chain_for(price: Decimal, ts: datetime = DECISION_TIME) -> OptionChain:
    expiry = LAST_DAY + timedelta(days=1)
    atm = (price / 25).to_integral_value() * 25
    quotes = []
    for k in range(-4, 5):
        strike = atm + 25 * k
        for right in Right:
            quotes.append(
                OptionQuote(
                    contract_id=f"{right.value}{strike}",
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    bid=D(150),
                    ask=D(152),
                    open_interest=500,
                )
            )
    return OptionChain("NQ", ts, price, tuple(quotes))
