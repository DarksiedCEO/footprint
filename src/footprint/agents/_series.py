"""Shared, deterministic helpers for the readers: input checks, EMA, relative volume."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from footprint.core.models import Bar, Score


def require_series(bars: Sequence[Bar], timeframe_minutes: int | None, field: str) -> None:
    """Reject input a reader could misread: empty, mixed symbols, out of order, wrong timeframe.

    With a timeframe, every gap between consecutive bars must be a whole number of
    bar lengths, so bars of a shorter timeframe handed to a slower reader are refused.
    """
    if not isinstance(bars, Sequence) or isinstance(bars, str | bytes):
        raise TypeError(f"{field} must be a sequence of Bar")
    if not bars:
        raise ValueError(f"{field} must not be empty")
    symbol = None
    for bar in bars:
        if not isinstance(bar, Bar):
            raise TypeError(f"{field} must contain only Bar, got {type(bar).__name__}")
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise ValueError(f"{field} mixes symbols {symbol!r} and {bar.symbol!r}")
    step = timedelta(minutes=timeframe_minutes) if timeframe_minutes is not None else None
    for earlier, later in pairwise(bars):
        gap = later.ts - earlier.ts
        if gap <= timedelta(0):
            raise ValueError(f"{field} must be in strictly increasing time order")
        if step is not None and gap % step:
            raise ValueError(
                f"{field}: gap {gap} between bars is not a multiple of {timeframe_minutes} min"
            )


def ema(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average seeded with the simple average of the first `period` values.

    Returns one value per input from index period-1 onward (empty if too few values).
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return []
    alpha = Decimal(2) / Decimal(period + 1)
    current = sum(values[:period], Decimal(0)) / Decimal(period)
    out = [current]
    for value in values[period:]:
        current = current + alpha * (value - current)
        out.append(current)
    return out


def relative_volume(
    bars: Sequence[Bar], index: int, lookback: int, timezone: str
) -> Decimal | None:
    """Volume of bars[index] divided by the average volume of the previous `lookback` bars
    that share its local clock time (same time of day on earlier sessions).

    None when there are fewer than `lookback` such bars or their average is zero:
    a reader treats "can't measure" as no signal, never as a spike.
    """
    zone = ZoneInfo(timezone)
    target = bars[index]
    clock = target.ts.astimezone(zone).time()
    same_time: list[int] = []
    for bar in reversed(bars[:index]):
        if bar.ts.astimezone(zone).time() == clock:
            same_time.append(bar.volume)
            if len(same_time) == lookback:
                break
    if len(same_time) < lookback:
        return None
    average = Decimal(sum(same_time)) / Decimal(lookback)
    if average == 0:
        return None
    return Decimal(target.volume) / average


def candle_direction(bar: Bar) -> Score:
    if bar.close > bar.open:
        return Score.BULLISH
    if bar.close < bar.open:
        return Score.BEARISH
    return Score.NEUTRAL
