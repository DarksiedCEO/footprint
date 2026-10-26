"""Shared, deterministic helpers for the readers: input checks, EMA, relative volume."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from zoneinfo import ZoneInfo

from footprint.core.models import Bar, Score
from footprint.core.series import require_series

__all__ = [
    "candle_direction",
    "ema",
    "relative_to_same_time",
    "relative_volume",
    "require_series",
    "same_time_indices",
    "swing_points",
]


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


def same_time_indices(bars: Sequence[Bar], index: int, lookback: int, timezone: str) -> list[int]:
    """Indices of up to `lookback` earlier bars sharing bars[index]'s local clock time,
    most recent first (the same time of day on earlier sessions).
    """
    zone = ZoneInfo(timezone)
    clock = bars[index].ts.astimezone(zone).time()
    found: list[int] = []
    for i in range(index - 1, -1, -1):
        if bars[i].ts.astimezone(zone).time() == clock:
            found.append(i)
            if len(found) == lookback:
                break
    return found


def relative_to_same_time(
    values: Sequence[Decimal], bars: Sequence[Bar], index: int, lookback: int, timezone: str
) -> Decimal | None:
    """values[index] divided by the average of `values` at the previous `lookback` bars
    that share bars[index]'s local clock time.

    None when there are fewer than `lookback` such bars or their average is zero:
    callers must treat "can't measure" explicitly, never as a normal reading.
    """
    earlier = same_time_indices(bars, index, lookback, timezone)
    if len(earlier) < lookback:
        return None
    average = sum((values[i] for i in earlier), Decimal(0)) / Decimal(lookback)
    if average == 0:
        return None
    return values[index] / average


def relative_volume(
    bars: Sequence[Bar], index: int, lookback: int, timezone: str
) -> Decimal | None:
    """Volume of bars[index] relative to the same time of day on the previous sessions."""
    volumes = [Decimal(bar.volume) for bar in bars]
    return relative_to_same_time(volumes, bars, index, lookback, timezone)


def swing_points(values: Sequence[Decimal], strength: int, *, high: bool) -> list[Decimal]:
    """Swing highs (or lows): values strictly above (below) the `strength` values on each side.

    Returned oldest first. The last `strength` values can never qualify, so a swing is
    only confirmed once `strength` later bars exist (no look-ahead).
    """
    found = []
    for i in range(strength, len(values) - strength):
        neighbours = [*values[i - strength : i], *values[i + 1 : i + strength + 1]]
        value = values[i]
        if (high and all(value > n for n in neighbours)) or (
            not high and all(value < n for n in neighbours)
        ):
            found.append(value)
    return found


def candle_direction(bar: Bar) -> Score:
    if bar.close > bar.open:
        return Score.BULLISH
    if bar.close < bar.open:
        return Score.BEARISH
    return Score.NEUTRAL
