"""Input checks shared by every floor that consumes a run of bars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from itertools import pairwise

from footprint.core.models import Bar


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
