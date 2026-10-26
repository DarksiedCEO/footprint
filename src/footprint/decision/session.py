"""Session guard (Cyrus): which trading window a moment falls in, news blackouts,
and when an overnight position must be flat.

Every time, weekday and holiday comes from the instrument profile or the caller;
nothing here knows any instrument's hours. Windows are judged to the minute in
the session's timezone: a window includes every second of its end minute.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from footprint.core.calendar import SessionKind, local_minute, session_at
from footprint.core.config import InstrumentProfile
from footprint.core.models import require_utc

__all__ = [
    "NewsBlackout",
    "SessionKind",
    "in_news_blackout",
    "overnight_must_be_flat",
    "session_at",
]


@dataclass(frozen=True, slots=True)
class NewsBlackout:
    """A no-new-entries window around a scheduled release, start and end inclusive.

    The window's edges are supplied by the caller (the economic calendar CSV, or the
    scheduled rules in decision/news_rules.py), so this module invents no lead or lag
    times. `time_stop`: an entry made just after the window ends gets the news time stop
    (execution.yaml news:); a daily scheduled window carries it only when Andre's
    N-OPEN-2 setting says so.
    """

    name: str
    start: datetime
    end: datetime
    time_stop: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"NewsBlackout.name must be a non-empty string, got {self.name!r}")
        if not isinstance(self.time_stop, bool):
            raise TypeError("NewsBlackout.time_stop must be a bool")
        require_utc(self.start, "NewsBlackout.start")
        require_utc(self.end, "NewsBlackout.end")
        if self.end < self.start:
            raise ValueError(f"NewsBlackout {self.name!r}: end must not be before start")

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


def in_news_blackout(moment: datetime, blackouts: Iterable[NewsBlackout]) -> NewsBlackout | None:
    """The blackout covering `moment`, or None."""
    require_utc(moment, "moment")
    for blackout in blackouts:
        if blackout.covers(moment):
            return blackout
    return None


def overnight_must_be_flat(moment: datetime, instrument: InstrumentProfile) -> bool:
    """True when an overnight position must already be closed.

    An overnight position may be held only strictly before the overnight
    session's last minute; at that minute, and at any moment outside an open
    overnight session, it must be flat.
    """
    if session_at(moment, instrument) is not SessionKind.OVERNIGHT:
        return True
    _, clock = local_minute(moment, instrument.overnight_session)
    return clock == instrument.overnight_session.end
