"""The play caller's regime reader: is today a TREND day, a CHOP day, or neither?

Settings: config/regime.yaml (Andre's pre-registration DRAFT, 2026-10-21; not yet
signed off and not yet tested on any data). The label is made once the first
`decision_minutes` of the cash session have closed, from completed 1-minute bars
only (a bar's `ts` is the minute it opens), so nothing after that moment can change it:

- opening efficiency: |last close - first open| over the window [cash open, cash open +
  decision_minutes), divided by the path length (sum of |close - previous close|, the
  first move measured from the first bar's open). Near 1 = a clean one-way push, up or
  down; near 0 = back and forth. An opening with no movement has no efficiency.
- its rank: the percent of the previous `efficiency_lookback_sessions` trading days
  whose opening efficiency is STRICTLY lower (ties do not count as lower).
- overnight range: high - low over [overnight open, cash open), against the median of
  the previous `overnight_lookback_sessions` trading days' overnight ranges.

TREND: rank >= trend_rank_pct AND overnight range >= that median.
CHOP: rank < chop_rank_pct.
STAND_ASIDE: anything else, and fail-closed on any missing or unusable input (outside
the prime session, before the decision time, thin, gappy, unordered, duplicated or
off-minute bars by the strategy's data settings, too little history, an opening with
no movement, history reaching outside the maintained calendar). CHOP does not use the overnight,
but an unusable overnight still stands aside: every input must be sound.

`opening_efficiency` and `classify` are pure, so research can label days on its own
session calendar with exactly the same rules.

History: a call reads `history_sessions` trading days of bars (today plus the longer
lookback), so the bars handed to it must reach back that far. A label made from sound
data is kept for the rest of its trading day while today's own bars (overnight and
opening) are unchanged, so later calls (with the oldest history day sliding out of a
fixed-length window) give the same answer; different bars for today mean a new label.
A STAND_ASIDE caused by missing or unusable data is NOT kept, so a late-arriving minute
can still be read (the label can then come later than the decision time; PC-4).
Corrected HISTORY bars with today's bars unchanged are not noticed: replaying a whole
corrected data set needs a new reader (one reader per run).

The reader only labels. Which play runs on which label: the play caller (decision/play.py).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, replace
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


def _start(bar: Bar) -> datetime:
    return bar.ts


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


@dataclass(frozen=True, slots=True)
class Classification:
    regime: Regime
    rank_pct: Decimal
    overnight_median: Decimal
    reason: str


def opening_efficiency(window: Sequence[Bar]) -> Decimal | None:
    """|last close - first open| / path length over `window`; None when nothing moved."""
    if not window:
        raise ValueError("opening_efficiency needs at least one bar")
    path = abs(window[0].close - window[0].open)
    for earlier, later in pairwise(window):
        path += abs(later.close - earlier.close)
    if path == 0:
        return None
    return abs(window[-1].close - window[0].open) / path


def classify(
    efficiency: Decimal,
    past_efficiencies: Sequence[Decimal],
    overnight_range: Decimal,
    past_overnight_ranges: Sequence[Decimal],
    settings: RegimeSettings,
) -> Classification:
    """The label from today's numbers and the previous sessions' (newest first or any
    order: only counts and the median are used). Both histories must be non-empty."""
    if not past_efficiencies or not past_overnight_ranges:
        raise ValueError("classify needs a non-empty history of both kinds")
    lower = sum(1 for value in past_efficiencies if value < efficiency)
    rank = _PERCENT_MAX * lower / len(past_efficiencies)
    median = statistics.median(past_overnight_ranges)
    if rank >= settings.trend_rank_pct:
        if overnight_range >= median:
            return Classification(Regime.TREND, rank, median, "clean opening after a wide night")
        return Classification(
            Regime.STAND_ASIDE,
            rank,
            median,
            f"trend-rank opening ({rank:.1f}) but the overnight range {overnight_range} is "
            f"below its median {median}",
        )
    if rank < settings.chop_rank_pct:
        return Classification(Regime.CHOP, rank, median, "back-and-forth opening")
    return Classification(
        Regime.STAND_ASIDE,
        rank,
        median,
        f"opening efficiency rank {rank:.1f} is neither trend nor chop",
    )


class _Unusable(Exception):
    """An input that cannot stand for what it should measure (the message says why)."""


class RegimeReader:
    def __init__(
        self, settings: RegimeSettings, data: DataSettings, instrument: InstrumentProfile
    ) -> None:
        cash = instrument.cash_session
        cash_minutes = (
            datetime.combine(date.min, cash.end) - datetime.combine(date.min, cash.start)
        ) // _MINUTE
        if cash.end <= cash.start or settings.decision_minutes >= cash_minutes:
            raise ValueError(
                f"decision_minutes ({settings.decision_minutes}) must end inside the cash "
                f"session ({cash.start} to {cash.end})"
            )
        self._s = settings
        self._data = data
        self._instrument = instrument
        self._zone = ZoneInfo(cash.timezone)
        self._night_zone = ZoneInfo(instrument.overnight_session.timezone)
        self._decided: dict[date, tuple[tuple[Bar, ...], RegimeCall]] = {}

    @property
    def agent_id(self) -> str:
        return "regime"

    @property
    def history_sessions(self) -> int:
        """Trading days of bars a call reads: today plus the longer lookback."""
        return max(self._s.efficiency_lookback_sessions, self._s.overnight_lookback_sessions) + 1

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> RegimeCall:
        require_utc(as_of, "as_of")
        today = trading_day_at(as_of, self._instrument)
        if today is None or session_at(as_of, self._instrument) is not SessionKind.PRIME:
            return RegimeCall(Regime.STAND_ASIDE, None, as_of, "outside the prime session")
        decision = self._cash_open(today) + _MINUTE * self._s.decision_minutes
        if as_of < decision:
            return RegimeCall(
                Regime.STAND_ASIDE, today, as_of, f"before the decision time {decision}"
            )
        today_bars = self._today_bars(minute_bars, today)
        kept = self._decided.get(today)
        if kept is not None and kept[0] == today_bars:
            return replace(kept[1], ts=as_of)
        result, sound = self._label(minute_bars, as_of, today)
        if sound:
            # Only today's label is ever needed again, and only while today's own bars
            # (overnight and opening) are the ones it was made from.
            self._decided = {today: (today_bars, result)}
        return result

    def _today_bars(self, minute_bars: Sequence[Bar], today: date) -> tuple[Bar, ...]:
        """Today's overnight and opening bars as given (unchecked): the key a kept label
        is valid for. Different bars for today (another data set, a corrected feed)
        mean a new label; history days sliding out of a fixed window do not."""
        # Every bar in [night open, decision) has closed by the time a label is asked for
        # (the call returns earlier before the decision time),
        # so no completeness filter is needed, and bisecting the input directly keeps a
        # cached call cheap. Unordered input gives some slice here, but such data is
        # refused when labelling, so it can never have produced a kept label.
        end = self._cash_open(today) + _MINUTE * self._s.decision_minutes
        first = bisect_left(minute_bars, self._night_open(today), key=_start)
        return tuple(minute_bars[first : bisect_left(minute_bars, end, key=_start)])

    # --- the label -----------------------------------------------------------------

    def _label(
        self, minute_bars: Sequence[Bar], as_of: datetime, today: date
    ) -> tuple[RegimeCall, bool]:
        """The call, and whether it rests on sound data (only then is it kept)."""

        def aside(reason: str, **known: Decimal | None) -> tuple[RegimeCall, bool]:
            return RegimeCall(Regime.STAND_ASIDE, today, as_of, reason, **known), False

        bars = completed(minute_bars, as_of, 1)
        starts = [bar.ts for bar in bars]
        if any(later <= earlier for earlier, later in pairwise(starts)):
            return aside("bars are out of order or duplicated")
        try:
            efficiency = opening_efficiency(self._opening(bars, starts, today))
        except _Unusable as exc:
            return aside(f"today's opening: {exc}")
        try:
            night = self._overnight_range(bars, starts, today)
        except _Unusable as exc:
            return aside(f"today's overnight: {exc}", efficiency=efficiency)
        if efficiency is None:
            return aside("today's opening did not move: no efficiency", overnight_range=night)

        past_eff: list[Decimal] = []
        past_night: list[Decimal] = []
        day = today
        for count in range(self.history_sessions - 1):
            try:
                day = previous_trading_day(day, self._instrument)
            except ValueError as exc:
                return aside(f"history: {exc}", efficiency=efficiency, overnight_range=night)
            if day < self._instrument.calendar_from:
                # Before the maintained calendar no holiday is known: a closed day would
                # be read as a trading day with missing bars, or worse, skipped silently.
                return aside(
                    f"history reaches {day.isoformat()}, before the maintained calendar "
                    f"({self._instrument.calendar_from.isoformat()})",
                    efficiency=efficiency,
                    overnight_range=night,
                )
            try:
                if count < self._s.efficiency_lookback_sessions:
                    value = opening_efficiency(self._opening(bars, starts, day))
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

        c = classify(efficiency, past_eff, night, past_night, self._s)
        call = RegimeCall(
            c.regime,
            today,
            as_of,
            c.reason,
            efficiency=efficiency,
            efficiency_rank_pct=c.rank_pct,
            overnight_range=night,
            overnight_median=c.overnight_median,
        )
        return call, True

    # --- windows -----------------------------------------------------------------

    def _cash_open(self, day: date) -> datetime:
        start = self._instrument.cash_session.start
        return datetime.combine(day, start, self._zone).astimezone(UTC)

    def _night_open(self, day: date) -> datetime:
        night = self._instrument.overnight_session
        start_day = day - timedelta(days=1) if night.start > night.end else day
        return datetime.combine(start_day, night.start, self._night_zone).astimezone(UTC)

    def _opening(self, bars: Sequence[Bar], starts: Sequence[datetime], day: date) -> Sequence[Bar]:
        begin = self._cash_open(day)
        end = begin + _MINUTE * self._s.decision_minutes
        return self._window(bars, starts, begin, end, need_last=True)

    def _overnight_range(
        self, bars: Sequence[Bar], starts: Sequence[datetime], day: date
    ) -> Decimal:
        window = self._window(
            bars, starts, self._night_open(day), self._cash_open(day), need_last=False
        )
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
        """The bars of [begin, end), or _Unusable when they cannot stand for it: a bar
        off the whole minute, below the minimum share of minutes, a run of missing
        minutes longer than max_gap_minutes (at the start, inside or at the end), or
        (need_last) without the final minute."""
        window = bars[bisect_left(starts, begin) : bisect_left(starts, end)]
        span = (end - begin) // _MINUTE
        if not window:
            raise _Unusable(f"0 of {span} minutes")
        if any(bar.ts.second or bar.ts.microsecond for bar in window):
            raise _Unusable("a bar is not on a whole minute")
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
