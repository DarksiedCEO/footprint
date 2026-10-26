"""Turn 1-minute bars into what the agents consume, using only completed bars.

Convention: a Bar's `ts` is the moment the bar OPENS. A bar of `m` minutes stamped
`t` covers [t, t + m) and is complete only once `as_of >= t + m`. Every function
here takes `as_of` and drops anything not yet complete, so no agent can see a
half-formed bar (G12): that is look-ahead in a backtest and a false signal live.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from footprint.core.config import Session
from footprint.core.models import Bar, require_utc
from footprint.core.series import BarSeries, BarWindow, require_series
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
    if isinstance(bars, BarWindow):
        # In time order, so the closed bars are a prefix: the same bars, found by search.
        end = bisect_right(
            bars.series.starts, as_of, bars.start, bars.end, key=lambda ts: ts + length
        )
        return bars if end == bars.end else bars.series.window(bars.start, end)
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
    if isinstance(source, BarWindow):
        return _resample_window(source, minutes, as_of)
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


@dataclass(frozen=True, slots=True)
class _Buckets:
    """A series cut into `minutes`-long buckets (as `_resample` cuts it): bucket g holds
    bars[firsts[g]:firsts[g + 1]], starts at starts[g], and merges to merged[g]."""

    firsts: list[int]  # one more entry than buckets: the series' length last
    starts: list[datetime]
    merged: list[Bar]


_BUCKETS: WeakKeyDictionary[BarSeries, dict[int, _Buckets]] = WeakKeyDictionary()


def _buckets(series: BarSeries, minutes: int) -> _Buckets:
    by_length = _BUCKETS.setdefault(series, {})
    found = by_length.get(minutes)
    if found is not None:
        return found
    length = timedelta(minutes=minutes)
    bars = series.bars
    firsts: list[int] = []
    starts: list[datetime] = []
    for i, bar in enumerate(bars):
        start = bar.ts - ((bar.ts - _EPOCH) % length)
        if not starts or start != starts[-1]:
            firsts.append(i)
            starts.append(start)
    firsts.append(len(bars))
    merged = [_merge(bars[firsts[g] : firsts[g + 1]], starts[g]) for g in range(len(starts))]
    by_length[minutes] = _Buckets(firsts, starts, merged)
    return by_length[minutes]


def _resample_window(source: BarWindow, minutes: int, as_of: datetime) -> list[Bar]:
    """`_resample` of a window: its buckets are the series' buckets cut to the window.
    A whole bucket is the series' merged bar; one the window cuts (its first or last)
    is merged from the window's own bars, so no bar outside the window is read."""
    b = _buckets(source.series, minutes)
    bars = source.series.bars
    length = timedelta(minutes=minutes)
    lo, hi = source.start, source.end
    first = bisect_right(b.firsts, lo) - 1  # the bucket holding the window's first bar
    last = bisect_left(b.firsts, hi)  # one past the bucket holding its last bar
    # Emitted: the buckets that have closed by as_of (their starts only grow).
    stop = bisect_right(b.starts, as_of, first, last, key=lambda ts: ts + length)
    out: list[Bar] = []
    g = first
    if g < stop and b.firsts[g] < lo:  # cut by the window's start (perhaps its end too)
        out.append(_merge(bars[lo : min(b.firsts[g + 1], hi)], b.starts[g]))
        g += 1
    tail: Bar | None = None
    if stop > g and b.firsts[stop] > hi:  # the last emitted bucket is cut by the end
        stop -= 1
        tail = _merge(bars[b.firsts[stop] : hi], b.starts[stop])
    out += b.merged[g:stop]
    if tail is not None:
        out.append(tail)
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


def _grouped_days(source: Sequence[Bar], session: Session) -> list[tuple[date, Bar]]:
    """The merged session bar of each local date with session bars in `source`."""
    grouped: dict[date, list[Bar]] = {}
    for bar in source:
        local = local_time(bar.ts, session.timezone)
        if _in_session(local.time(), session):
            grouped.setdefault(local.date(), []).append(bar)
    return [(day, _merge(grouped[day], grouped[day][0].ts)) for day in sorted(grouped)]


def _session_days(bars: Sequence[Bar], session: Session, as_of: datetime) -> SessionDays:
    """Daily bars from the session's completed 1-minute bars, grouped by local date."""
    require_utc(as_of, "as_of")
    zone = ZoneInfo(session.timezone)
    source = completed(bars, as_of, 1)
    if isinstance(source, BarWindow) and source:
        merged_days = _window_days(source, session)
    else:
        merged_days = _grouped_days(source, session)
    local_now = as_of.astimezone(zone)
    # At exactly `end` the last minute has closed, so the day is complete.
    in_progress = session.start <= local_now.time() < session.end
    days: list[Bar] = []
    today: Bar | None = None
    for day, merged in merged_days:
        if in_progress and day == local_now.date():
            today = merged
        else:
            days.append(merged)
    return SessionDays(days=tuple(days), today=today)


@dataclass(frozen=True, slots=True)
class _Days:
    """A series' session bars by local date (as `_session_days` groups them): day d
    holds the bars at `members[d]` (ascending indices), all inside [lows[d], highs[d]),
    and merges to merged[d]. The days' spans do not overlap and ascend."""

    dates: list[date]
    members: list[array[int]]
    lows: list[int]
    highs: list[int]
    merged: list[Bar]


_DAYS: WeakKeyDictionary[BarSeries, dict[Session, _Days | None]] = WeakKeyDictionary()


def _days(series: BarSeries, session: Session) -> _Days | None:
    """None when the series' local dates do not run in time order (a clock change
    across midnight): then the days' spans overlap and cannot be searched."""
    by_session = _DAYS.setdefault(series, {})
    if session in by_session:
        return by_session[session]
    zone = ZoneInfo(session.timezone)
    grouped: dict[date, array[int]] = {}
    for i, bar in enumerate(series.bars):
        local = bar.ts.astimezone(zone)
        if _in_session(local.time(), session):
            grouped.setdefault(local.date(), array("q")).append(i)
    dates = sorted(grouped)
    members = [grouped[d] for d in dates]
    lows = [m[0] for m in members]
    highs = [m[-1] + 1 for m in members]
    bars = series.bars
    usable = all(lows[k] >= highs[k - 1] for k in range(1, len(dates)))
    by_session[session] = (
        _Days(
            dates=dates,
            members=members,
            lows=lows,
            highs=highs,
            merged=[_merge([bars[i] for i in m], bars[m[0]].ts) for m in members],
        )
        if usable
        else None
    )
    return by_session[session]


def _window_days(source: BarWindow, session: Session) -> list[tuple[date, Bar]]:
    """The merged session bar of each local date with session bars in the window, by
    date. A date the window holds whole is the series' merged bar; a date it cuts is
    merged from the window's own bars."""
    d = _days(source.series, session)
    if d is None:  # the series' local dates do not run in order: as for any tuple
        return _grouped_days(source, session)
    bars = source.series.bars
    lo, hi = source.start, source.end
    out: list[tuple[date, Bar]] = []
    for k in range(bisect_right(d.highs, lo), len(d.dates)):
        if d.lows[k] >= hi:
            break
        if lo <= d.lows[k] and d.highs[k] <= hi:
            out.append((d.dates[k], d.merged[k]))
            continue
        inside = [bars[i] for i in d.members[k] if lo <= i < hi]
        if inside:
            out.append((d.dates[k], _merge(inside, inside[0].ts)))
    return out
