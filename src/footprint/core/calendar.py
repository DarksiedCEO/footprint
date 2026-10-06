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
    """The exchange trades `day` (a weekday it trades, not one of its holidays)."""
    return day.weekday() in instrument.trading_days and day not in instrument.holidays


def is_tradable_day(day: date, instrument: InstrumentProfile) -> bool:
    """Footprint may trade `day`: the exchange trades it, it is not a no-trade day,
    and it is inside the maintained calendar. Fail closed: past `calendar_through`
    no holiday or early close is known, so no day there trades (remediation item 7)."""
    return (
        is_trading_day(day, instrument)
        and day not in instrument.no_trade_days
        and day <= instrument.calendar_through
    )


def previous_trading_day(day: date, instrument: InstrumentProfile) -> date:
    """The last day strictly before `day` that the exchange traded and Footprint did
    not treat as closed (a no-trade day's stub session never supplies the prior day)."""
    candidate = day
    for _ in range(_MAX_SEARCH_DAYS):
        candidate -= timedelta(days=1)
        if is_trading_day(candidate, instrument) and candidate not in instrument.no_trade_days:
            return candidate
    raise ValueError(f"no trading day in the {_MAX_SEARCH_DAYS} days before {day}")


def prime_close(day: date, instrument: InstrumentProfile) -> time:
    """When the prime session of `day` ends: its early close, or the normal end."""
    return instrument.early_closes.get(day, instrument.prime_session.end)


def cash_close(day: date, instrument: InstrumentProfile) -> time:
    """When the cash session of `day` ends (an early close cuts it short)."""
    return min(instrument.cash_session.end, prime_close(day, instrument))


def entry_cutoff(moment: datetime, instrument: InstrumentProfile, lead_minutes: int) -> datetime:
    """`lead_minutes` before the prime close of `moment`'s local day (an early close
    included): from then on no new entries, and anything open is flattened."""
    require_utc(moment, "moment")
    prime = instrument.prime_session
    zone = ZoneInfo(prime.timezone)
    day = moment.astimezone(zone).date()
    close = datetime.combine(day, prime_close(day, instrument), zone)
    return close - timedelta(minutes=lead_minutes)


def calendar_warning(today: date, instrument: InstrumentProfile) -> str | None:
    """What to tell Andre when the exchange calendar (holidays, early closes) is about
    to run out, or has: past `calendar_through` every day is treated as closed (no
    trading at all) until the calendar is extended."""
    through = instrument.calendar_through
    if today > through:
        return (
            f"EXCHANGE CALENDAR EXPIRED on {through}: NO NEW TRADES (every later day is "
            "treated as closed) until instruments/nq.yaml is extended with the exchange's "
            "holidays and early closes and the program is restarted"
        )
    left = (through - today).days
    if left <= instrument.calendar_warn_days:
        return (
            f"exchange calendar ends {through} ({left} days): add the exchange's next holidays and "
            "early closes to instruments/nq.yaml"
        )
    return None


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
        if is_tradable_day(day, instrument) and clock <= prime_close(day, instrument):
            return SessionKind.PRIME, day
        return SessionKind.CLOSED, None  # a holiday, a no-trade day, or after an early close
    day, clock = local_minute(moment, overnight)
    if _covers(overnight, clock):
        crosses_midnight = overnight.start > overnight.end
        trading_day = (
            day + timedelta(days=1) if crosses_midnight and clock >= overnight.start else day
        )
        if is_tradable_day(trading_day, instrument):
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
