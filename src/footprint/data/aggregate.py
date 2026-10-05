"""Turn 1-minute bars into what the agents consume, using only completed bars.

Convention: a Bar's `ts` is the moment the bar OPENS. A bar of `m` minutes stamped
`t` covers [t, t + m) and is complete only once `as_of >= t + m`. Every function
here takes `as_of` and drops anything not yet complete, so no agent can see a
half-formed bar (G12): that is look-ahead in a backtest and a false signal live.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from footprint.core.config import Session
from footprint.core.models import Bar, require_utc
from footprint.core.series import require_series
from footprint.core.timecache import local_time

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def completed(bars: Sequence[Bar], as_of: datetime, timeframe_minutes: int) -> tuple[Bar, ...]:
    """The bars of `timeframe_minutes` that had fully closed by `as_of`, as a tuple.
    A tuple whose bars have all closed is returned as the same object, so the series
    caches (keyed on the tuple) are shared by every reader of one decision."""
    require_utc(as_of, "as_of")
    if timeframe_minutes < 1:
        raise ValueError("timeframe_minutes must be >= 1")
    length = timedelta(minutes=timeframe_minutes)
    if isinstance(bars, tuple) and all(bar.ts + length <= as_of for bar in bars):
        return bars
    return tuple(bar for bar in bars if bar.ts + length <= as_of)


# Results per (tuple of bars, arguments). Tuples only (they cannot change); kept alive
# here so an id is never reused while cached. Lists are always recomputed.
_RESAMPLED: list[tuple[tuple[Bar, ...], int, datetime, tuple[Bar, ...]]] = []
_SESSION_DAYS: list[tuple[tuple[Bar, ...], Session, datetime, SessionDays]] = []
_KEEP = 4


def resample(bars: Sequence[Bar], minutes: int, as_of: datetime) -> list[Bar]:
    """See `_resample`; identical, with results remembered for a tuple of bars."""
    if not isinstance(bars, tuple):
        return _resample(bars, minutes, as_of)
    for seen, size, moment, result in _RESAMPLED:
        if seen is bars and size == minutes and moment == as_of:
            return list(result)
    out = _resample(bars, minutes, as_of)
    _RESAMPLED.append((bars, minutes, as_of, tuple(out)))
    del _RESAMPLED[:-_KEEP]
    return out


def _resample(bars: Sequence[Bar], minutes: int, as_of: datetime) -> list[Bar]:
    """Combine 1-minute bars into completed `minutes`-long bars.

    Buckets are aligned to whole multiples of `minutes` since the Unix epoch, so
    buckets start on the clock's natural boundaries for that length. A bucket is emitted only once
    it has closed by `as_of`; a bucket with missing minutes is still emitted from
    the minutes that exist, because the exchange does not print empty minutes.
    """
    require_utc(as_of, "as_of")
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    source = completed(bars, as_of, 1)
    if not source:
        return []
    require_series(source, 1, "resample bars")
    length = timedelta(minutes=minutes)
    out: list[Bar] = []
    bucket: list[Bar] = []
    bucket_start: datetime | None = None
    for bar in source:
        start = bar.ts - ((bar.ts - _EPOCH) % length)
        if start != bucket_start:
            if bucket and bucket_start is not None and bucket_start + length <= as_of:
                out.append(_merge(bucket, bucket_start))
            bucket, bucket_start = [], start
        bucket.append(bar)
    if bucket and bucket_start is not None and bucket_start + length <= as_of:
        out.append(_merge(bucket, bucket_start))
    return out


def _merge(bars: Sequence[Bar], ts: datetime) -> Bar:
    return Bar(
        symbol=bars[0].symbol,
        ts=ts,
        open=bars[0].open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=bars[-1].close,
        volume=sum(bar.volume for bar in bars),
    )


@dataclass(frozen=True, slots=True)
class SessionDays:
    """One bar per local date built from a session's minutes.

    `days` are completed sessions, oldest first. `today` is the session in progress
    at `as_of` (None before it opens or after it has closed for the day, in which
    case the day's session is the last entry of `days`).
    """

    days: tuple[Bar, ...]
    today: Bar | None


def _in_session(clock: time, session: Session) -> bool:
    """A bar stamped `clock` trades inside the session: start <= clock < end.

    A bar stamped exactly at `end` opens after the close, so it is excluded.
    """
    if session.start >= session.end:
        raise ValueError("daily session bars need a session that does not cross midnight")
    return session.start <= clock < session.end


def session_days(bars: Sequence[Bar], session: Session, as_of: datetime) -> SessionDays:
    """See `_session_days`; identical, with results remembered for a tuple of bars."""
    if not isinstance(bars, tuple):
        return _session_days(bars, session, as_of)
    for seen, which, moment, result in _SESSION_DAYS:
        if seen is bars and which == session and moment == as_of:
            return result
    out = _session_days(bars, session, as_of)
    _SESSION_DAYS.append((bars, session, as_of, out))
    del _SESSION_DAYS[:-_KEEP]
    return out


def _session_days(bars: Sequence[Bar], session: Session, as_of: datetime) -> SessionDays:
    """Daily bars from the session's completed 1-minute bars, grouped by local date."""
    require_utc(as_of, "as_of")
    zone = ZoneInfo(session.timezone)
    grouped: dict[date, list[Bar]] = {}
    for bar in completed(bars, as_of, 1):
        local = local_time(bar.ts, session.timezone)
        if _in_session(local.time(), session):
            grouped.setdefault(local.date(), []).append(bar)
    local_now = as_of.astimezone(zone)
    # At exactly `end` the last minute has closed, so the day is complete.
    in_progress = session.start <= local_now.time() < session.end
    days: list[Bar] = []
    today: Bar | None = None
    for day in sorted(grouped):
        merged = _merge(grouped[day], grouped[day][0].ts)
        if in_progress and day == local_now.date():
            today = merged
        else:
            days.append(merged)
    return SessionDays(days=tuple(days), today=today)
