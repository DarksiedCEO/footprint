"""Input checks shared by every floor that consumes a run of bars, and the checked
series a backtest takes its decision windows from (BH-11)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Self

from footprint.core.models import Bar

# Tuples already validated (kept alive here, so an id is never reused while cached).
# A tuple of frozen Bars cannot change, so validating it once is enough; lists are
# always re-checked.
_VALIDATED: list[tuple[tuple[Bar, ...], int | None]] = []
_KEEP = 4


def require_series(bars: Sequence[Bar], timeframe_minutes: int | None, field: str) -> None:
    """Reject input a reader could misread: empty, mixed symbols, out of order, wrong timeframe.

    With a timeframe, every gap between consecutive bars must be a whole number of
    bar lengths, so bars of a shorter timeframe handed to a slower reader are refused.
    """
    if isinstance(bars, BarWindow) and bars and bars.series.covers(timeframe_minutes):
        return  # a non-empty run of consecutive bars of a checked series
    if isinstance(bars, tuple) and any(
        seen is bars and frame == timeframe_minutes for seen, frame in _VALIDATED
    ):
        return
    _check(bars, timeframe_minutes, field)
    if isinstance(bars, tuple):
        _VALIDATED.append((bars, timeframe_minutes))
        del _VALIDATED[:-_KEEP]


def _check(bars: Sequence[Bar], timeframe_minutes: int | None, field: str) -> None:
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


class BarSeries:
    """A run of bars checked once (`require_series` with `timeframe_minutes`), with
    each bar's start time, so the windows taken from it need no second check and can
    be searched by time (BH-11: the backtest's decision windows)."""

    def __init__(self, bars: Sequence[Bar], timeframe_minutes: int, field: str) -> None:
        self.bars: tuple[Bar, ...] = tuple(bars)
        _check(self.bars, timeframe_minutes, field)
        self.timeframe_minutes = timeframe_minutes
        self.starts: list[datetime] = [bar.ts for bar in self.bars]

    def covers(self, timeframe_minutes: int | None) -> bool:
        """Every run of consecutive bars of this series passes `require_series` with
        `timeframe_minutes` (None: no spacing check; else the series' own)."""
        return timeframe_minutes is None or timeframe_minutes == self.timeframe_minutes

    def window(self, start: int, end: int) -> BarWindow:
        """bars[start:end] as a BarWindow."""
        return BarWindow(self, start, end)


class BarWindow(tuple[Bar, ...]):
    """`series.bars[start:end]`: to every reader a plain tuple of exactly those bars.
    It also says where they lie in their series, so the series helpers can reuse work
    done once per series. They only ever use it for bars inside the window, so a
    window never shows a bar it does not hold (no look-ahead). Its bars are in strictly
    increasing time order (the series was checked)."""

    series: BarSeries
    start: int

    def __new__(cls, series: BarSeries, start: int, end: int) -> Self:
        if not 0 <= start <= end <= len(series.bars):
            raise ValueError(f"window [{start}, {end}) is outside a series of {len(series.bars)}")
        window = super().__new__(cls, series.bars[start:end])
        window.series = series
        window.start = start
        return window

    @property
    def end(self) -> int:
        """One past the window's last bar, as an index into the series."""
        return self.start + len(self)
