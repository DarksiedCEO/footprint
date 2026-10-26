"""Nia (zero-day): flags a market that matches nothing in recent history.

Nia never looks for a trade. She runs deterministic tripwires on the latest
completed 1-minute bar and on today's cash-session range, each measured against
its typical value over the previous `lookback_sessions` sessions (G9). Any
tripwire, or any reading that cannot be measured, trips her; a tripped Nia means
stand down (no new entries), never tighten-and-trade.

She judges only inside the prime session of a trading day, on a fresh feed;
anywhere else she trips. The bar tripwires look at each of the last `recent_bars`
completed bars (the momentum trigger itself spans two bars), so an extreme bar
still trips her for a few minutes after it prints. The bar before the last (the
trigger's spike candidate) is judged for jump and volatility at its own, higher
spike_bar_trip_multiple, so the strategy's own trigger candle is not vetoed for
being a big candle. Tripwires:
- range: today's range since the prime open vs the same clock window on earlier
  trading days,
- volume: the bar's volume vs the same time of day (its own, higher multiple),
- volatility: the bar's true range vs the same time of day,
- jump: the bar's close-to-close move vs the same time of day.
The option-spread tripwire is not measured in v1 (no option-quote history);
Felix's spread cap stands in for it.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from footprint.agents._series import relative_at
from footprint.core.calendar import SessionKind, is_trading_day, session_at
from footprint.core.config import DataSettings, InstrumentProfile, ZeroDaySettings
from footprint.core.models import Bar, require_utc
from footprint.core.series import BarSeries, BarWindow, require_series
from footprint.core.timecache import local_time
from footprint.data.aggregate import completed

_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class ZeroDayCheck:
    ts: datetime
    tripped: bool
    findings: tuple[str, ...]


class ZeroDayAgent:
    def __init__(
        self, settings: ZeroDaySettings, data: DataSettings, instrument: InstrumentProfile
    ) -> None:
        self._settings = settings
        self._data = data
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "zeroday"

    def check(self, minute_bars: Sequence[Bar], as_of: datetime) -> ZeroDayCheck:
        require_utc(as_of, "as_of")
        if session_at(as_of, self._instrument) is not SessionKind.PRIME:  # early closes too
            return ZeroDayCheck(as_of, True, ("outside the prime session: not judged",))
        bars = completed(minute_bars, as_of, 1)
        if len(bars) < 2:
            return ZeroDayCheck(as_of, True, ("too few completed bars to judge the market",))
        require_series(bars, 1, "ZeroDayAgent bars")
        age = as_of - (bars[-1].ts + _MINUTE)
        if age > timedelta(seconds=self._data.max_bar_age_seconds):
            return ZeroDayCheck(as_of, True, (f"stale feed: newest bar closed {age} ago",))
        s = self._settings
        tz = self._instrument.prime_session.timezone
        last = len(bars) - 1
        trips: list[str] = []
        notes: list[str] = []

        def judge(name: str, ratio: Decimal | None, limit: Decimal) -> None:
            if ratio is None:
                trips.append(f"{name}: cannot establish a typical value")
            elif ratio > limit:
                trips.append(f"{name}: {ratio:.2f}x typical (limit {limit}x)")
            else:
                notes.append(f"{name} {ratio:.2f}x")

        recent = range(max(0, last - s.recent_bars + 1), last + 1)

        def judge_recent(
            name: str, value: Callable[[int], Decimal], limit: Decimal, spike_limit: Decimal
        ) -> None:
            """Judge each recent bar against `limit`; the bar before the last (the
            trigger's spike candidate) against `spike_limit`. Reports the worst one.
            value(i) is the measure of bar i (only the bars compared are measured).
            """
            worst: tuple[Decimal, Decimal] | None = None
            for i in recent:
                ratio = relative_at(value, bars, i, s.lookback_sessions, tz)
                if ratio is None:
                    judge(name, None, limit)
                    return
                bar_limit = spike_limit if i == last - 1 else limit
                if worst is None or ratio / bar_limit > worst[0] / worst[1]:
                    worst = (ratio, bar_limit)
            if worst is not None:
                judge(name, worst[0], worst[1])

        def volume(i: int) -> Decimal:
            return Decimal(bars[i].volume)

        def true_range(i: int) -> Decimal:  # the first bar has no previous close
            if i == 0:
                return bars[0].high - bars[0].low
            prev, cur = bars[i - 1], bars[i]
            return max(cur.high, prev.close) - min(cur.low, prev.close)

        def jump(i: int) -> Decimal:
            return Decimal(0) if i == 0 else abs(bars[i].close - bars[i - 1].close)

        judge_recent("volume", volume, s.volume_trip_multiple, s.volume_trip_multiple)
        judge_recent("volatility", true_range, s.trip_multiple, s.spike_bar_trip_multiple)
        judge_recent("jump", jump, s.trip_multiple, s.spike_bar_trip_multiple)

        today_range, typical = self._range_since_open(bars, as_of)
        judge("range", today_range / typical if typical else None, s.trip_multiple)

        notes.append("spread: not measured in v1")
        if trips:
            return ZeroDayCheck(as_of, True, tuple(trips + notes))
        return ZeroDayCheck(as_of, False, tuple(notes))

    def _range_since_open(
        self, bars: Sequence[Bar], as_of: datetime
    ) -> tuple[Decimal, Decimal | None]:
        """Today's range from the prime open to `as_of`, and the average range over the
        same clock window on the previous `lookback_sessions` trading days (None when
        any of them, or today, has no bars in that window).
        """
        prime = self._instrument.prime_session
        zone = ZoneInfo(prime.timezone)
        local_now = as_of.astimezone(zone)
        cutoff = local_now.time()
        spans = (
            self._window_spans(bars, cutoff)
            if isinstance(bars, BarWindow)
            else self._spans(bars, cutoff)
        )
        today = spans.pop(local_now.date(), None)
        if today is None:
            return Decimal(0), None
        lookback = self._settings.lookback_sessions
        history = [spans[d] for d in sorted(spans) if d < local_now.date()][-lookback:]
        if len(history) < lookback:
            return today, None
        typical = sum(history, Decimal(0)) / Decimal(lookback)
        return today, typical or None

    def _counted(self, day: date) -> bool:
        """A day whose range counts: the exchange traded it and it is not a no-trade
        day (a stub session: not a day)."""
        return is_trading_day(day, self._instrument) and day not in self._instrument.no_trade_days

    def _spans(self, bars: Sequence[Bar], cutoff: time) -> dict[date, Decimal]:
        """Each counted day's range over its bars from the prime open to `cutoff`."""
        prime = self._instrument.prime_session
        by_day: dict[date, list[Bar]] = {}
        for bar in bars:
            local = local_time(bar.ts, prime.timezone)
            day = local.date()
            if prime.start <= local.time() < cutoff and self._counted(day):
                by_day.setdefault(local.date(), []).append(bar)
        return {day: _span(day_bars) for day, day_bars in by_day.items()}

    def _window_spans(self, window: BarWindow, cutoff: time) -> dict[date, Decimal]:
        """`_spans` of a window: a day the window holds whole (every bar of it from the
        prime open on) takes its range from the series' running highs and lows; a day
        it cuts is measured from the window's own bars, as `_spans` would."""
        prime = self._instrument.prime_session
        days = _prime_days(window.series, prime.timezone, prime.start)
        if days is None:  # the series' local dates do not run in order: as for a list
            return self._spans(window, cutoff)
        zone = ZoneInfo(prime.timezone)
        bars = window.series.bars

        def clock(i: int) -> time:
            return bars[i].ts.astimezone(zone).time()

        lo, hi = window.start, window.end
        out: dict[date, Decimal] = {}
        for k in range(bisect_right(days.highs, lo), len(days.dates)):
            if days.lows[k] >= hi:
                break
            day = days.dates[k]
            if not self._counted(day):
                continue
            members = days.members[k]
            if lo <= days.lows[k] and days.highs[k] <= hi and days.ordered[k]:
                n = bisect_left(members, cutoff, key=clock)  # its bars before `cutoff`
                if n:
                    out[day] = days.running_high[k][n - 1] - days.running_low[k][n - 1]
                continue
            inside = [bars[i] for i in members if lo <= i < hi and clock(i) < cutoff]
            if inside:
                out[day] = _span(inside)
        return out


@dataclass(frozen=True, slots=True)
class _PrimeDays:
    """A series' bars from the prime open on, by local date: day d holds the bars at
    `members[d]` (ascending indices, all inside [lows[d], highs[d]); the days' spans
    do not overlap and ascend). When `ordered[d]` (their local clock times strictly
    rise), the bars before a cutoff are a prefix, and running_high/low[d][n - 1] is the
    max/min over its first n bars as max() and min() pick it (the first of equals)."""

    dates: list[date]
    members: list[array[int]]
    lows: list[int]
    highs: list[int]
    ordered: list[bool]
    running_high: list[list[Decimal]]
    running_low: list[list[Decimal]]


_PRIME_DAYS: WeakKeyDictionary[BarSeries, dict[tuple[str, time], _PrimeDays | None]] = (
    WeakKeyDictionary()
)


def _prime_days(series: BarSeries, timezone: str, start: time) -> _PrimeDays | None:
    """None when the series' local dates do not run in time order (a clock change
    across midnight): then the days' spans overlap and cannot be searched."""
    by_key = _PRIME_DAYS.setdefault(series, {})
    if (timezone, start) in by_key:
        return by_key[(timezone, start)]
    zone = ZoneInfo(timezone)
    members: dict[date, array[int]] = {}
    clocks: dict[date, list[time]] = {}
    for i, bar in enumerate(series.bars):
        local = bar.ts.astimezone(zone)
        if start <= local.time():
            members.setdefault(local.date(), array("q")).append(i)
            clocks.setdefault(local.date(), []).append(local.time())
    dates = sorted(members)
    lows = [members[d][0] for d in dates]
    highs = [members[d][-1] + 1 for d in dates]
    if any(lows[k] < highs[k - 1] for k in range(1, len(dates))):
        by_key[(timezone, start)] = None
        return None
    running_high: list[list[Decimal]] = []
    running_low: list[list[Decimal]] = []
    for day in dates:
        top: list[Decimal] = []
        bottom: list[Decimal] = []
        for i in members[day]:
            bar = series.bars[i]
            top.append(bar.high if not top or bar.high > top[-1] else top[-1])
            bottom.append(bar.low if not bottom or bar.low < bottom[-1] else bottom[-1])
        running_high.append(top)
        running_low.append(bottom)
    by_key[(timezone, start)] = _PrimeDays(
        dates=dates,
        members=[members[d] for d in dates],
        lows=lows,
        highs=highs,
        ordered=[all(a < b for a, b in pairwise(clocks[d])) for d in dates],
        running_high=running_high,
        running_low=running_low,
    )
    return by_key[(timezone, start)]


def _span(bars: Sequence[Bar]) -> Decimal:
    return max(b.high for b in bars) - min(b.low for b in bars)
