"""Session guard (Cyrus): which trading window a moment falls in, news blackouts,
and when an overnight position must be flat.

Every time and window comes from the instrument profile or the caller; nothing
here knows any instrument's hours.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from footprint.core.config import InstrumentProfile, Session


def _require_utc(ts: datetime, field: str) -> None:
    if not isinstance(ts, datetime):
        raise TypeError(f"{field} must be a datetime, got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be timezone-aware UTC, got {ts!r}")


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
        _require_utc(self.start, "NewsBlackout.start")
        _require_utc(self.end, "NewsBlackout.end")
        if self.end < self.start:
            raise ValueError(f"NewsBlackout {self.name!r}: end must not be before start")

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


def _local_clock(moment: datetime, session: Session) -> time:
    _require_utc(moment, "moment")
    return moment.astimezone(ZoneInfo(session.timezone)).time().replace(second=0, microsecond=0)


def _covers(session: Session, clock: time) -> bool:
    if session.start <= session.end:
        return session.start <= clock <= session.end
    return clock >= session.start or clock <= session.end


def session_at(moment: datetime, instrument: InstrumentProfile) -> SessionKind:
    """The window `moment` (UTC) falls in, judged to the minute in the session's timezone."""
    if _covers(instrument.prime_session, _local_clock(moment, instrument.prime_session)):
        return SessionKind.PRIME
    if _covers(instrument.overnight_session, _local_clock(moment, instrument.overnight_session)):
        return SessionKind.OVERNIGHT
    return SessionKind.CLOSED


def in_news_blackout(moment: datetime, blackouts: Iterable[NewsBlackout]) -> NewsBlackout | None:
    """The blackout covering `moment`, or None."""
    _require_utc(moment, "moment")
    for blackout in blackouts:
        if blackout.covers(moment):
            return blackout
    return None


def overnight_must_be_flat(moment: datetime, instrument: InstrumentProfile) -> bool:
    """True when an overnight position must already be closed.

    An overnight position may be held only strictly before the overnight
    session's last minute; at that minute, and at any moment outside the
    overnight session, it must be flat.
    """
    overnight = instrument.overnight_session
    clock = _local_clock(moment, overnight)
    if session_at(moment, instrument) is not SessionKind.OVERNIGHT:
        return True
    return clock == overnight.end
