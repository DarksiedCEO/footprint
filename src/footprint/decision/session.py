"""Session guard (Cyrus): which trading window a moment falls in, news blackouts,
and when an overnight position must be flat.

Every time, weekday and holiday comes from the instrument profile or the caller;
nothing here knows any instrument's hours. Windows are judged to the minute in
the session's timezone: a window includes every second of its end minute.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from footprint.core.calendar import is_trading_day
from footprint.core.config import InstrumentProfile, Session
from footprint.core.models import require_utc


class SessionKind(Enum):
    PRIME = "PRIME"
    OVERNIGHT = "OVERNIGHT"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class NewsBlackout:
    """A no-new-entries window around a scheduled release, start and end inclusive.

    The window's edges are supplied by the caller (from the economic calendar),
    so this module invents no lead or lag times.
    """

    name: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"NewsBlackout.name must be a non-empty string, got {self.name!r}")
        require_utc(self.start, "NewsBlackout.start")
        require_utc(self.end, "NewsBlackout.end")
        if self.end < self.start:
            raise ValueError(f"NewsBlackout {self.name!r}: end must not be before start")

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


def _local(moment: datetime, session: Session) -> tuple[date, time]:
    require_utc(moment, "moment")
    local = moment.astimezone(ZoneInfo(session.timezone))
    return local.date(), local.time().replace(second=0, microsecond=0)


def _covers(session: Session, clock: time) -> bool:
    if session.start <= session.end:
        return session.start <= clock <= session.end
    return clock >= session.start or clock <= session.end


def session_at(moment: datetime, instrument: InstrumentProfile) -> SessionKind:
    """The window `moment` (UTC) falls in, or CLOSED on non-trading days.

    A PRIME window belongs to its own calendar day. An OVERNIGHT window that
    crosses midnight belongs to the trading day it leads into, so the evening
    before a weekend or holiday is CLOSED.
    """
    prime, overnight = instrument.prime_session, instrument.overnight_session
    day, clock = _local(moment, prime)
    if _covers(prime, clock):
        return SessionKind.PRIME if is_trading_day(day, instrument) else SessionKind.CLOSED
    day, clock = _local(moment, overnight)
    if _covers(overnight, clock):
        crosses_midnight = overnight.start > overnight.end
        trading_day = (
            day + timedelta(days=1) if crosses_midnight and clock >= overnight.start else day
        )
        if is_trading_day(trading_day, instrument):
            return SessionKind.OVERNIGHT
    return SessionKind.CLOSED


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
    _, clock = _local(moment, instrument.overnight_session)
    return clock == instrument.overnight_session.end
