"""The play caller's regime reader: is today a TREND day, a CHOP day, or neither?

Settings: config/regime.yaml (Andre's pre-registration DRAFT, 2026-10-21; not yet
signed off and not yet tested on any data). The label is made once the first
`decision_minutes` of the cash session have closed, from completed 1-minute bars
only (a bar's `ts` is the minute it opens), so nothing after that moment can change it:

- opening efficiency: |last close - first open| over the window [cash open, cash open +
  decision_minutes), divided by the path length (sum of |close - previous close|, the
  first move measured from the first bar's open). Near 1 = a clean one-way push; near
  0 = back and forth. An opening with no movement has no efficiency.
- its rank: the percent of the previous `efficiency_lookback_sessions` trading days
  whose opening efficiency is STRICTLY lower (ties do not count as lower).
- overnight range: high - low over [overnight open, cash open), against the median of
  the previous `overnight_lookback_sessions` trading days' overnight ranges.

TREND: rank >= trend_rank_pct AND overnight range >= that median.
CHOP: rank < chop_rank_pct.
STAND_ASIDE: anything else, and fail-closed on any missing or unusable input (outside
the prime session, before the decision time, thin or gappy windows by the strategy's
data settings, too little history, an opening with no movement).

The reader only labels. Which play runs on which label is the console's job (not wired).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from zoneinfo import ZoneInfo

from footprint.core.calendar import SessionKind, previous_trading_day, session_at, trading_day_at
from footprint.core.config import (
    _PERCENT_MAX,
    DataSettings,
    InstrumentProfile,
    RegimeSettings,
)
from footprint.core.models import Bar, require_utc
from footprint.data.aggregate import completed

_MINUTE = timedelta(minutes=1)


class Regime(Enum):
    TREND = "trend"
    CHOP = "chop"
    STAND_ASIDE = "stand_aside"


@dataclass(frozen=True, slots=True)
class RegimeCall:
    regime: Regime
    day: date | None
    ts: datetime
    reason: str
    efficiency: Decimal | None = None
    efficiency_rank_pct: Decimal | None = None
    overnight_range: Decimal | None = None
    overnight_median: Decimal | None = None


class _Unusable(Exception):
    """A window that cannot stand for what it should measure (the message says why)."""


class RegimeReader:
    def __init__(
        self, settings: RegimeSettings, data: DataSettings, instrument: InstrumentProfile
    ) -> None:
        self._s = settings
        self._data = data
        self._instrument = instrument
        self._zone = ZoneInfo(instrument.cash_session.timezone)
        self._night_zone = ZoneInfo(instrument.overnight_session.timezone)

    @property
    def agent_id(self) -> str:
        return "regime"

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> RegimeCall:
        require_utc(as_of, "as_of")
        if session_at(as_of, self._instrument) is not SessionKind.PRIME:
            return RegimeCall(Regime.STAND_ASIDE, None, as_of, "outside the prime session")
        today = trading_day_at(as_of, self._instrument)
        assert today is not None  # PRIME always belongs to a trading day
        decision = self._cash_open(today) + _MINUTE * self._s.decision_minutes
        if as_of < decision:
            return RegimeCall(
                Regime.STAND_ASIDE, today, as_of, f"before the decision time {decision}"
            )

        bars = completed(minute_bars, as_of, 1)
        starts = [bar.ts for bar in bars]

        def aside(reason: str, **known: Decimal | None) -> RegimeCall:
            return RegimeCall(Regime.STAND_ASIDE, today, as_of, reason, **known)

        try:
            efficiency = self._efficiency(bars, starts, today)
        except _Unusable as exc:
            return aside(f"today's opening: {exc}")
        try:
            night = self._overnight_range(bars, starts, today)
        except _Unusable as exc:
            return aside(f"today's overnight: {exc}", efficiency=efficiency)
        if efficiency is None:
            return aside("today's opening did not move: no efficiency", overnight_range=night)

        lookback = max(self._s.efficiency_lookback_sessions, self._s.overnight_lookback_sessions)
        past_eff: list[Decimal] = []
        past_night: list[Decimal] = []
        day = today
        for count in range(lookback):
            day = previous_trading_day(day, self._instrument)
            try:
                if count < self._s.efficiency_lookback_sessions:
                    value = self._efficiency(bars, starts, day)
                    if value is None:
                        raise _Unusable("the opening did not move: no efficiency")
                    past_eff.append(value)
                if count < self._s.overnight_lookback_sessions:
                    past_night.append(self._overnight_range(bars, starts, day))
            except _Unusable as exc:
                return aside(
                    f"history {day.isoformat()}: {exc}",
                    efficiency=efficiency,
                    overnight_range=night,
                )

        lower = sum(1 for value in past_eff if value < efficiency)
        rank = _PERCENT_MAX * lower / len(past_eff)
        median = statistics.median(past_night)
        known = {
            "efficiency": efficiency,
            "efficiency_rank_pct": rank,
            "overnight_range": night,
            "overnight_median": median,
        }
        if rank >= self._s.trend_rank_pct:
            if night >= median:
                return RegimeCall(
                    Regime.TREND, today, as_of, "clean opening after a wide night", **known
                )
            return aside(
                f"trend-rank opening ({rank:.1f}) but the overnight range {night} is below "
                f"its median {median}",
                **known,
            )
        if rank < self._s.chop_rank_pct:
            return RegimeCall(Regime.CHOP, today, as_of, "back-and-forth opening", **known)
        return aside(f"opening efficiency rank {rank:.1f} is neither trend nor chop", **known)

    # --- windows -----------------------------------------------------------------

    def _cash_open(self, day: date) -> datetime:
        return datetime.combine(day, self._instrument.cash_session.start, self._zone).astimezone(
            UTC
        )

    def _efficiency(
        self, bars: Sequence[Bar], starts: Sequence[datetime], day: date
    ) -> Decimal | None:
        begin = self._cash_open(day)
        end = begin + _MINUTE * self._s.decision_minutes
        window = self._window(bars, starts, begin, end, need_last=True)
        path = abs(window[0].close - window[0].open)
        for earlier, later in pairwise(window):
            path += abs(later.close - earlier.close)
        if path == 0:
            return None
        return abs(window[-1].close - window[0].open) / path

    def _overnight_range(
        self, bars: Sequence[Bar], starts: Sequence[datetime], day: date
    ) -> Decimal:
        night = self._instrument.overnight_session
        start_day = day - timedelta(days=1) if night.start > night.end else day
        begin = datetime.combine(start_day, night.start, self._night_zone).astimezone(UTC)
        window = self._window(bars, starts, begin, self._cash_open(day), need_last=False)
        return max(bar.high for bar in window) - min(bar.low for bar in window)

    def _window(
        self,
        bars: Sequence[Bar],
        starts: Sequence[datetime],
        begin: datetime,
        end: datetime,
        *,
        need_last: bool,
    ) -> Sequence[Bar]:
        """The bars of [begin, end), or _Unusable when they cannot stand for it: below the
        minimum share of minutes, a run of missing minutes longer than max_gap_minutes
        (at the start, inside or at the end), or (need_last) without the final minute."""
        window = bars[bisect_left(starts, begin) : bisect_left(starts, end)]
        span = (end - begin) // _MINUTE
        if not window:
            raise _Unusable(f"0 of {span} minutes")
        need = Decimal(span) * self._data.min_session_coverage_pct / _PERCENT_MAX
        if Decimal(len(window)) < need:
            raise _Unusable(f"{len(window)} of {span} minutes")
        limit = timedelta(minutes=self._data.max_gap_minutes)
        gaps = [window[0].ts - begin, end - (window[-1].ts + _MINUTE)]
        gaps += [later.ts - earlier.ts - _MINUTE for earlier, later in pairwise(window)]
        if max(gaps) > limit:
            raise _Unusable(f"{max(gaps)} of consecutive missing minutes")
        if need_last and window[-1].ts + _MINUTE != end:
            raise _Unusable("the final minute is missing")
        return window
