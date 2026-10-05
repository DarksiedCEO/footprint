"""Grant (levels): how much room a trade has before the next opposing level.

Levels tracked each session (G10): prior cash-session high/low/close, the overnight
session's high/low, VWAP anchored at the cash open, and the trend timeframe's swing
highs/lows. Round numbers are tracked and reported but are NOT walls in v1: with
the profit target needing more points than the round-number spacing, the next round
number is always closer than the target and every trade would be vetoed (holds
while N exceeds the spacing, i.e. one contract; Andre to decide).

Scoring against N, the underlying points needed for the profit target:
distance to the nearest opposing level >= room_multiple x N  -> +1 (the console
weights it); below wall_multiple x N -> -1, which the console treats as a veto;
in between -> 0. A level exactly at the price is a wall at distance zero. No
opposing level at all means unlimited room. Deviations are listed in
docs/decisions-pending.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from footprint.agents._series import swing_points
from footprint.core.calendar import (
    SessionKind,
    is_trading_day,
    previous_trading_day,
    session_at,
)
from footprint.core.config import (
    DataSettings,
    InstrumentProfile,
    LevelsSettings,
    TrendSettings,
)
from footprint.core.models import Bar, Direction, Read, Score, require_utc
from footprint.core.series import require_series
from footprint.data.aggregate import completed, resample

_MINUTE = timedelta(minutes=1)


def points_for_target(
    profit_target: Decimal, assumed_delta: Decimal, point_value: Decimal, contracts: int
) -> Decimal:
    """N: underlying points that make `profit_target` on the option position (G4)."""
    if contracts < 1:
        raise ValueError("contracts must be >= 1")
    for name, value in (
        ("profit_target", profit_target),
        ("assumed_delta", assumed_delta),
        ("point_value", point_value),
    ):
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite number > 0, got {value}")
    return profit_target / (assumed_delta * point_value * Decimal(contracts))


@dataclass(frozen=True, slots=True)
class Level:
    name: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class LevelsSnapshot:
    """Every level known at `as_of`. `walls` are the ones that can block a trade."""

    as_of: datetime
    price: Decimal
    vwap: Decimal
    walls: tuple[Level, ...]
    round_below: Decimal
    round_above: Decimal


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Levels could not be computed; the console must not trade."""

    as_of: datetime
    reason: str


class LevelsReader:
    def __init__(
        self,
        settings: LevelsSettings,
        trend: TrendSettings,
        data: DataSettings,
        instrument: InstrumentProfile,
    ) -> None:
        self._settings = settings
        self._trend = trend
        self._data = data
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "levels"

    def snapshot(self, minute_bars: Sequence[Bar], as_of: datetime) -> LevelsSnapshot | Unavailable:
        """Levels at `as_of` from 1-minute bars (trend bars are built from them).

        Only bars complete at `as_of` are used. Fails closed (Unavailable) outside the
        prime session of a trading day, on a stale feed, and when the previous trading
        day's cash session, the overnight session, today's cash session so far or the
        trend window is missing, thin or gappy.
        """
        require_utc(as_of, "as_of")
        inst = self._instrument
        prime, cash = inst.prime_session, inst.cash_session
        zone = ZoneInfo(prime.timezone)
        local_now = as_of.astimezone(zone)
        today = local_now.date()
        if not (is_trading_day(today, inst) and prime.start <= local_now.time() <= prime.end):
            return Unavailable(as_of, "outside the prime session of a trading day")

        minutes = completed(minute_bars, as_of, 1)
        if not minutes:
            return Unavailable(as_of, "no completed 1-minute bars")
        require_series(minutes, 1, "LevelsReader minute bars")
        age = as_of - (minutes[-1].ts + _MINUTE)
        if age > timedelta(seconds=self._data.max_bar_age_seconds):
            return Unavailable(as_of, f"stale feed: newest completed bar closed {age} ago")

        prior_day = previous_trading_day(today, inst)
        prior_open = datetime.combine(prior_day, cash.start, zone).astimezone(UTC)
        prior_close = datetime.combine(prior_day, cash.end, zone).astimezone(UTC)
        prior_bars = _between(minutes, prior_open, prior_close)
        problem = self._window_problem(prior_bars, prior_open, prior_close, need_last=True)
        if problem:
            return Unavailable(as_of, f"previous trading day {prior_day} cash session: {problem}")

        today_open = datetime.combine(today, cash.start, zone).astimezone(UTC)
        today_close = datetime.combine(today, cash.end, zone).astimezone(UTC)
        if as_of <= today_open:
            return Unavailable(as_of, "cash session has not opened: no VWAP yet")
        today_cash = _between(minutes, today_open, today_close)
        problem = self._window_problem(
            today_cash, today_open, min(as_of, today_close), need_last=False
        )
        if problem:
            return Unavailable(as_of, f"today's cash session (VWAP): {problem}")
        volume = sum(bar.volume for bar in today_cash)
        if volume == 0:
            return Unavailable(as_of, "zero volume since the cash open: no VWAP")
        vwap = sum(
            ((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in today_cash),
            Decimal(0),
        ) / Decimal(volume)

        night_open, night_close = self._overnight_window(today)
        overnight = _between(minutes, night_open, night_close)
        problem = self._window_problem(overnight, night_open, night_close, need_last=False)
        if problem:
            return Unavailable(as_of, f"overnight session before today: {problem}")

        tf_minutes = self._trend.timeframe_minutes
        frame = timedelta(minutes=tf_minutes)
        lookback = self._settings.swing_lookback_bars
        recent = resample(minutes, tf_minutes, as_of)[-lookback:]
        if len(recent) < lookback:
            return Unavailable(
                as_of, f"only {len(recent)} of {lookback} completed trend bars for swing levels"
            )
        for earlier, later in pairwise(recent):
            missing = earlier.ts + frame
            while missing < later.ts:
                if session_at(missing + _MINUTE, inst) is not SessionKind.CLOSED:
                    return Unavailable(as_of, f"gap in trend bars: no bucket at {missing}")
                missing += frame
        strength = self._trend.swing_strength

        walls = [
            Level("prior_high", max(bar.high for bar in prior_bars)),
            Level("prior_low", min(bar.low for bar in prior_bars)),
            Level("prior_close", prior_bars[-1].close),
            Level("overnight_high", max(bar.high for bar in overnight)),
            Level("overnight_low", min(bar.low for bar in overnight)),
            Level("vwap", vwap),
        ]
        walls += [
            Level("swing_high", price)
            for price in swing_points([b.high for b in recent], strength, high=True)
        ]
        walls += [
            Level("swing_low", price)
            for price in swing_points([b.low for b in recent], strength, high=False)
        ]

        price = minutes[-1].close
        step = inst.round_number_step
        below = (price / step).to_integral_value(ROUND_FLOOR) * step
        above = (price / step).to_integral_value(ROUND_CEILING) * step
        if below == price:
            below, above = price - step, price + step
        return LevelsSnapshot(
            as_of=as_of,
            price=price,
            vwap=vwap,
            walls=tuple(walls),
            round_below=below,
            round_above=above,
        )

    def _window_problem(
        self, bars: Sequence[Bar], begin: datetime, end: datetime, *, need_last: bool
    ) -> str | None:
        """Why `bars` cannot stand for the window [begin, end), or None if they can.

        Requires the minimum share of minutes, no run of missing minutes longer than
        max_gap_minutes at the start, inside or at the end, and (need_last) the final
        minute itself, so a session's close is its real close.
        """
        span = int((end - begin).total_seconds() // 60)
        if not bars:
            return f"0 of {span} minutes"
        need = Decimal(span) * self._data.min_session_coverage_pct.scaleb(-2)
        if Decimal(len(bars)) < need:
            return f"{len(bars)} of {span} minutes"
        limit = timedelta(minutes=self._data.max_gap_minutes)
        gaps = [bars[0].ts - begin, end - (bars[-1].ts + _MINUTE)]
        gaps += [later.ts - earlier.ts - _MINUTE for earlier, later in pairwise(bars)]
        if max(gaps) > limit:
            return f"{max(gaps)} of consecutive missing minutes"
        if need_last and bars[-1].ts + _MINUTE != end:
            return "the final minute is missing"
        return None

    def assess(
        self, snapshot: LevelsSnapshot | Unavailable, direction: Direction, n_points: Decimal
    ) -> Read:
        """+1 room to run, 0 tight, -1 a wall before the target (a veto)."""
        if not n_points.is_finite() or n_points <= 0:
            raise ValueError(f"n_points must be a finite number > 0, got {n_points}")
        if isinstance(snapshot, Unavailable):
            return Read(
                self.agent_id, snapshot.as_of, Score.BEARISH, f"unavailable: {snapshot.reason}"
            )
        price = snapshot.price
        if direction is Direction.LONG:
            ahead = [(lv.price - price, lv) for lv in snapshot.walls if lv.price >= price]
        else:
            ahead = [(price - lv.price, lv) for lv in snapshot.walls if lv.price <= price]
        if not ahead:
            return Read(
                self.agent_id, snapshot.as_of, Score.BULLISH, "no opposing level: open room"
            )
        distance, nearest = min(ahead, key=lambda pair: pair[0])
        room = distance / n_points
        detail = f"{nearest.name} {distance} pts away = {room:.2f}N (N={n_points:.2f})"
        if room >= self._settings.room_multiple:
            return Read(self.agent_id, snapshot.as_of, Score.BULLISH, f"room: {detail}")
        if room < self._settings.wall_multiple:
            return Read(self.agent_id, snapshot.as_of, Score.BEARISH, f"wall: {detail}")
        return Read(self.agent_id, snapshot.as_of, Score.NEUTRAL, f"tight: {detail}")

    def _overnight_window(self, today: date) -> tuple[datetime, datetime]:
        """[open, close) in UTC of the overnight session that led into `today`."""
        night = self._instrument.overnight_session
        zone = ZoneInfo(night.timezone)
        start_day = today - timedelta(days=1) if night.start > night.end else today
        begin = datetime.combine(start_day, night.start, zone)
        finish = datetime.combine(today, night.end, zone) + _MINUTE
        return begin.astimezone(UTC), finish.astimezone(UTC)


def _between(bars: Sequence[Bar], begin: datetime, end: datetime) -> list[Bar]:
    return [bar for bar in bars if begin <= bar.ts < end]
