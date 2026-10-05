"""Nia (zero-day): flags a market that matches nothing in recent history.

Nia never looks for a trade. She runs deterministic tripwires on the latest
completed 1-minute bar and on today's cash-session range, each measured against
its typical value over the previous `lookback_sessions` sessions (G9). Any
tripwire, or any reading that cannot be measured, trips her; a tripped Nia means
stand down (no new entries), never tighten-and-trade.

Tripwires:
- range: today's cash range so far vs the average completed cash-session range
  (only once the cash session has opened),
- volume: the bar's volume vs the same time of day (its own, higher multiple),
- volatility: the bar's true range vs the same time of day,
- jump: the bar's close-to-close move vs the same time of day.
The option-spread tripwire is not measured in v1 (no option-quote history);
Felix's spread cap stands in for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from footprint.agents._series import relative_to_same_time
from footprint.core.config import InstrumentProfile, ZeroDaySettings
from footprint.core.models import Bar, require_utc
from footprint.core.series import require_series
from footprint.data.aggregate import completed, session_days


@dataclass(frozen=True, slots=True)
class ZeroDayCheck:
    ts: datetime
    tripped: bool
    findings: tuple[str, ...]


class ZeroDayAgent:
    def __init__(self, settings: ZeroDaySettings, instrument: InstrumentProfile) -> None:
        self._settings = settings
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "zeroday"

    def check(self, minute_bars: Sequence[Bar], as_of: datetime) -> ZeroDayCheck:
        require_utc(as_of, "as_of")
        bars = completed(minute_bars, as_of, 1)
        if len(bars) < 2:
            return ZeroDayCheck(as_of, True, ("too few completed bars to judge the market",))
        require_series(bars, 1, "ZeroDayAgent bars")
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

        days = session_days(bars, self._instrument.cash_session, as_of)
        if days.today is None:
            notes.append("range: cash session not open")
        else:
            history = days.days[-s.lookback_sessions :]
            if len(history) < s.lookback_sessions:
                judge("range", None, s.trip_multiple)
            else:
                typical = sum((d.high - d.low for d in history), Decimal(0)) / len(history)
                today_range = days.today.high - days.today.low
                judge("range", today_range / typical if typical else None, s.trip_multiple)

        notes.append("spread: not measured in v1")
        if trips:
            return ZeroDayCheck(as_of, True, tuple(trips + notes))
        return ZeroDayCheck(as_of, False, tuple(notes))
