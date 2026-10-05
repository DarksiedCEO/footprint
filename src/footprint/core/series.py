"""Input checks shared by every floor that consumes a run of bars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from itertools import pairwise

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
