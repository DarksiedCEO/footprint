"""Q2: does an exported minute history have a bar for every minute Footprint could trade?

    first_gap(bars, start, instrument, max_missing, until=None)  -> None, or what is missing

A minute is tradable when the instrument's calendar (`session_at`) puts it in the PRIME
or OVERNIGHT window. Minutes the calendar calls CLOSED (the daily halt, weekends,
holidays, after an early close) are never gaps. Every minute from `start` to the last
bar is looked at, and with `until` (the moment the bars were read) on to the last minute
that had closed by then, so a missing tail is a gap too (G29-2). A run of more than
`max_missing` consecutive tradable minutes without a bar is a gap (a closed minute or a
bar ends a run). A short run is allowed because a window includes its end minute, when
the exchange has already halted, and a quiet minute may have no trade.

Fails closed: no bars at all is a gap, and bars outside the maintained calendar
(`calendar_from`..`calendar_through`, where the calendar cannot say which minutes trade)
are refused rather than waved through.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from footprint.core.calendar import SessionKind, session_at
from footprint.core.config import InstrumentProfile
from footprint.core.models import Bar, require_utc

_MINUTE = timedelta(minutes=1)


def first_gap(
    bars: Sequence[Bar],
    start: datetime,
    instrument: InstrumentProfile,
    max_missing: int,
    *,
    until: datetime | None = None,
) -> str | None:
    """The first run of more than `max_missing` tradable minutes with no bar, from `start`
    (a whole UTC minute) to the last bar, or to the last minute closed by `until` when
    that is later, said in words; None when there is none."""
    require_utc(start, "start")
    if start.second or start.microsecond:
        raise ValueError(f"start {start.isoformat()} must be a whole minute")
    if until is not None:
        require_utc(until, "until")
    if not bars:
        return "no bars at all"
    end = bars[-1].ts
    if until is not None:  # a minute has closed once its end is not after `until`
        end = max(end, until.replace(second=0, microsecond=0) - _MINUTE)
    first_day, last_day = min(start, bars[0].ts).date(), end.date()
    if first_day < instrument.calendar_from or last_day > instrument.calendar_through:
        return (
            f"{first_day}..{last_day} is outside the maintained calendar "
            f"({instrument.calendar_from}..{instrument.calendar_through}): which minutes "
            "trade there is unknown"
        )
    have = {b.ts for b in bars}
    run_from: datetime | None = None
    run = 0
    minute = start
    while minute <= end:
        if minute in have or session_at(minute, instrument) is SessionKind.CLOSED:
            if run > max_missing:
                break
            run, run_from = 0, None
        else:
            if run_from is None:
                run_from = minute
            run += 1
        minute += _MINUTE
    if run > max_missing and run_from is not None:
        return (
            f"{run} tradable minute(s) from {run_from.isoformat()} have no bar "
            f"(at most {max_missing} allowed)"
        )
    return None
