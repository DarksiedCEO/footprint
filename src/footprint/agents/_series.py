"""Shared, deterministic helpers for the readers: input checks, EMA, relative volume."""

from __future__ import annotations

from array import array
from bisect import bisect_left
from collections.abc import Callable, Sequence
from datetime import time
from decimal import Decimal
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from footprint.core.models import Bar, Score
from footprint.core.series import BarSeries, BarWindow, require_series
from footprint.core.timecache import local_time

__all__ = [
    "candle_direction",
    "ema",
    "last_two_swing_points",
    "relative_at",
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


# Per tuple of bars: local clock time -> ascending positions. Tuples only (they cannot
# change); kept alive here so an id is never reused while cached.
_CLOCK_INDEX: list[tuple[tuple[Bar, ...], str, dict[time, list[int]]]] = []
_KEEP = 4


def _clock_index(bars: tuple[Bar, ...], timezone: str) -> dict[time, list[int]]:
    for seen, zone, index in _CLOCK_INDEX:
        if seen is bars and zone == timezone:
            return index
    built: dict[time, list[int]] = {}
    for i, bar in enumerate(bars):
        built.setdefault(local_time(bar.ts, timezone).time(), []).append(i)
    _CLOCK_INDEX.append((bars, timezone, built))
    del _CLOCK_INDEX[:-_KEEP]
    return built


# Per checked series (a backtest's): the same clock index over the whole series, which
# every window taken from it searches inside its own bounds.
_SERIES_CLOCK: WeakKeyDictionary[BarSeries, dict[str, dict[time, array[int]]]] = WeakKeyDictionary()


def _series_clock(series: BarSeries, timezone: str) -> dict[time, array[int]]:
    by_zone = _SERIES_CLOCK.setdefault(series, {})
    found = by_zone.get(timezone)
    if found is None:
        zone = ZoneInfo(timezone)
        found = {}
        for i, bar in enumerate(series.bars):
            found.setdefault(bar.ts.astimezone(zone).time(), array("q")).append(i)
        by_zone[timezone] = found
    return found


def same_time_indices(bars: Sequence[Bar], index: int, lookback: int, timezone: str) -> list[int]:
    """Indices of up to `lookback` earlier bars sharing bars[index]'s local clock time,
    most recent first (the same time of day on earlier sessions).
    """
    clock = local_time(bars[index].ts, timezone).time()
    if isinstance(bars, BarWindow) and lookback >= 1 and 0 <= index < len(bars):
        # The series' positions of this clock time, inside [window start, the bar).
        found_at = _series_clock(bars.series, timezone)[clock]
        lo = bisect_left(found_at, bars.start)
        hi = bisect_left(found_at, bars.start + index, lo)
        return [p - bars.start for p in found_at[max(lo, hi - lookback) : hi][::-1]]
    if isinstance(bars, tuple) and lookback >= 1 and 0 <= index < len(bars):
        positions = _clock_index(bars, timezone)[clock]
        earlier = positions[: bisect_left(positions, index)]
        return earlier[-lookback:][::-1]
    found: list[int] = []
    for i in range(index - 1, -1, -1):
        if local_time(bars[i].ts, timezone).time() == clock:
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
    return relative_at(values.__getitem__, bars, index, lookback, timezone)


def relative_at(
    value: Callable[[int], Decimal], bars: Sequence[Bar], index: int, lookback: int, timezone: str
) -> Decimal | None:
    """`relative_to_same_time` with value(i) standing for values[i]: only the bars it
    compares are valued (a whole window's values are never built)."""
    earlier = same_time_indices(bars, index, lookback, timezone)
    if len(earlier) < lookback:
        return None
    average = sum((value(i) for i in earlier), Decimal(0)) / Decimal(lookback)
    if average == 0:
        return None
    return value(index) / average


def relative_volume(
    bars: Sequence[Bar], index: int, lookback: int, timezone: str
) -> Decimal | None:
    """Volume of bars[index] relative to the same time of day on the previous sessions."""
    return relative_at(lambda i: Decimal(bars[i].volume), bars, index, lookback, timezone)


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


def last_two_swing_points(values: Sequence[Decimal], strength: int, *, high: bool) -> list[Decimal]:
    """The last two of swing_points(values, strength, high=high) (fewer when there are
    fewer), oldest first: the same test, run from the end, stopping at the second."""
    found: list[Decimal] = []
    for i in range(len(values) - strength - 1, strength - 1, -1):
        neighbours = [*values[i - strength : i], *values[i + 1 : i + strength + 1]]
        value = values[i]
        if (high and all(value > n for n in neighbours)) or (
            not high and all(value < n for n in neighbours)
        ):
            found.append(value)
            if found[1:]:
                break
    return found[::-1]


def candle_direction(bar: Bar) -> Score:
    if bar.close > bar.open:
        return Score.BULLISH
    if bar.close < bar.open:
        return Score.BEARISH
    return Score.NEUTRAL
