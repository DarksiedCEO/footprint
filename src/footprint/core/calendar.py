"""The instrument's trading calendar: which local dates trade and which window a
moment falls in. Windows are judged to the minute in the session's timezone: a
window includes every second of its end minute.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from footprint.core.config import InstrumentProfile, Session
from footprint.core.models import require_utc

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


class SessionKind(Enum):
    PRIME = "PRIME"
    OVERNIGHT = "OVERNIGHT"
    CLOSED = "CLOSED"


def local_minute(moment: datetime, session: Session) -> tuple[date, time]:
    require_utc(moment, "moment")
    local = moment.astimezone(ZoneInfo(session.timezone))
    return local.date(), local.time().replace(second=0, microsecond=0)


def _covers(session: Session, clock: time) -> bool:
    if session.start <= session.end:
        return session.start <= clock <= session.end
    return clock >= session.start or clock <= session.end


def _classify(moment: datetime, instrument: InstrumentProfile) -> tuple[SessionKind, date | None]:
    prime, overnight = instrument.prime_session, instrument.overnight_session
    day, clock = local_minute(moment, prime)
    if _covers(prime, clock):
        if is_trading_day(day, instrument):
            return SessionKind.PRIME, day
        return SessionKind.CLOSED, None
    day, clock = local_minute(moment, overnight)
    if _covers(overnight, clock):
        crosses_midnight = overnight.start > overnight.end
        trading_day = (
            day + timedelta(days=1) if crosses_midnight and clock >= overnight.start else day
        )
        if is_trading_day(trading_day, instrument):
            return SessionKind.OVERNIGHT, trading_day
    return SessionKind.CLOSED, None


def session_at(moment: datetime, instrument: InstrumentProfile) -> SessionKind:
    """The window `moment` (UTC) falls in, or CLOSED on non-trading days.

    A PRIME window belongs to its own calendar day. An OVERNIGHT window that
    crosses midnight belongs to the trading day it leads into, so the evening
    before a weekend or holiday is CLOSED.
    """
    return _classify(moment, instrument)[0]


def trading_day_at(moment: datetime, instrument: InstrumentProfile) -> date | None:
    """The trading day `moment` belongs to (an overnight session belongs to the day it
    leads into), or None when the market is closed.
    """
    return _classify(moment, instrument)[1]
