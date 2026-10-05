"""The instrument's trading calendar: which local dates trade."""

from __future__ import annotations

from datetime import date, timedelta

from footprint.core.config import InstrumentProfile

_MAX_SEARCH_DAYS = 31  # give up after a month without a trading day


def is_trading_day(day: date, instrument: InstrumentProfile) -> bool:
    return day.weekday() in instrument.trading_days and day not in instrument.holidays


def previous_trading_day(day: date, instrument: InstrumentProfile) -> date:
    """The last trading day strictly before `day`."""
    candidate = day
    for _ in range(_MAX_SEARCH_DAYS):
        candidate -= timedelta(days=1)
        if is_trading_day(candidate, instrument):
            return candidate
    raise ValueError(f"no trading day in the {_MAX_SEARCH_DAYS} days before {day}")
