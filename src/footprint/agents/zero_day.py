"""Nia (zero-day): flags a market that matches nothing in recent history.

Nia never looks for a trade. She runs deterministic tripwires on the latest
completed 1-minute bar and on today's cash-session range, each measured against
its typical value over the previous `lookback_sessions` sessions (G9). Any
tripwire, or any reading that cannot be measured, trips her; a tripped Nia means
stand down (no new entries), never tighten-and-trade.

She judges only inside the prime session of a trading day, on a fresh feed;
anywhere else she trips. Tripwires:
- range: today's range since the prime open vs the same clock window on earlier
  trading days,
- volume: the bar's volume vs the same time of day (its own, higher multiple),
- volatility: the bar's true range vs the same time of day,
- jump: the bar's close-to-close move vs the same time of day.
The option-spread tripwire is not measured in v1 (no option-quote history);
Felix's spread cap stands in for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from footprint.agents._series import relative_to_same_time
from footprint.core.calendar import is_trading_day
from footprint.core.config import DataSettings, InstrumentProfile, ZeroDaySettings
from footprint.core.models import Bar, require_utc
from footprint.core.series import require_series
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
        prime = self._instrument.prime_session
        local_now = as_of.astimezone(ZoneInfo(prime.timezone))
        if not (
            is_trading_day(local_now.date(), self._instrument)
            and prime.start <= local_now.time() <= prime.end
        ):
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

        volumes = [Decimal(bar.volume) for bar in bars]
        judge(
            "volume",
            relative_to_same_time(volumes, bars, last, s.lookback_sessions, tz),
            s.volume_trip_multiple,
        )
        true_ranges = [bars[0].high - bars[0].low] + [
            max(cur.high, prev.close) - min(cur.low, prev.close) for prev, cur in pairwise(bars)
        ]
        judge(
            "volatility",
            relative_to_same_time(true_ranges, bars, last, s.lookback_sessions, tz),
            s.trip_multiple,
        )
        jumps = [Decimal(0)] + [abs(cur.close - prev.close) for prev, cur in pairwise(bars)]
        judge(
            "jump",
            relative_to_same_time(jumps, bars, last, s.lookback_sessions, tz),
            s.trip_multiple,
        )

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
        by_day: dict[date, list[Bar]] = {}
        for bar in bars:
            local = bar.ts.astimezone(zone)
            if prime.start <= local.time() < cutoff and is_trading_day(
                local.date(), self._instrument
            ):
                by_day.setdefault(local.date(), []).append(bar)
        today = by_day.pop(local_now.date(), [])
        if not today:
            return Decimal(0), None
        lookback = self._settings.lookback_sessions
        history = [by_day[d] for d in sorted(by_day) if d < local_now.date()][-lookback:]
        if len(history) < lookback:
            return _span(today), None
        typical = sum((_span(day) for day in history), Decimal(0)) / Decimal(lookback)
        return _span(today), typical or None


def _span(bars: Sequence[Bar]) -> Decimal:
    return max(b.high for b in bars) - min(b.low for b in bars)
