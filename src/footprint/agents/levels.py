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
from zoneinfo import ZoneInfo

from footprint.agents._series import swing_points
from footprint.core.calendar import is_trading_day, previous_trading_day
from footprint.core.config import (
    DataSettings,
    InstrumentProfile,
    LevelsSettings,
    TrendSettings,
)
from footprint.core.models import Bar, Direction, Read, Score, require_utc
from footprint.core.series import require_series
from footprint.data.aggregate import completed

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

    def snapshot(
        self, minute_bars: Sequence[Bar], trend_bars: Sequence[Bar], as_of: datetime
    ) -> LevelsSnapshot | Unavailable:
        """Levels at `as_of` from 1-minute bars and trend-timeframe bars.

        Only bars complete at `as_of` are used. Fails closed (Unavailable) outside the
        prime session of a trading day, on a stale feed, and when the previous trading
        day's cash session, the overnight session, today's VWAP or a full window of
        trend bars is missing or too thin.
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
        prior_bars = [
            bar
            for bar in minutes
            if bar.ts.astimezone(zone).date() == prior_day
            and cash.start <= bar.ts.astimezone(zone).time() < cash.end
        ]
        cash_minutes = _span_minutes(
            datetime.combine(prior_day, cash.start, zone),
            datetime.combine(prior_day, cash.end, zone),
        )
        if not self._covered(len(prior_bars), cash_minutes):
            return Unavailable(
                as_of,
                f"previous trading day {prior_day} cash session has {len(prior_bars)} of "
                f"{cash_minutes} minutes",
            )

        today_cash = [
            bar
            for bar in minutes
            if bar.ts.astimezone(zone).date() == today
            and cash.start <= bar.ts.astimezone(zone).time() < cash.end
        ]
        if not today_cash:
            return Unavailable(as_of, "cash session has not opened: no VWAP yet")
        volume = sum(bar.volume for bar in today_cash)
        if volume == 0:
            return Unavailable(as_of, "zero volume since the cash open: no VWAP")
        vwap = sum(
            ((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in today_cash),
            Decimal(0),
        ) / Decimal(volume)

        overnight, night_minutes = self._overnight_bars(minutes, today)
        if not self._covered(len(overnight), night_minutes):
            return Unavailable(
                as_of,
                f"overnight session before today has {len(overnight)} of {night_minutes} minutes",
            )

        tf_minutes = self._trend.timeframe_minutes
        trend = completed(trend_bars, as_of, tf_minutes)
        lookback = self._settings.swing_lookback_bars
        if len(trend) < lookback:
            return Unavailable(
                as_of, f"only {len(trend)} of {lookback} completed trend bars for swing levels"
            )
        require_series(trend, tf_minutes, "LevelsReader trend bars")
        frame = timedelta(minutes=tf_minutes)
        if as_of - (trend[-1].ts + frame) >= frame:
            return Unavailable(as_of, "stale trend bars: the latest closed bucket is missing")
        recent = trend[-lookback:]
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

    def _covered(self, have: int, expected: int) -> bool:
        need = Decimal(expected) * self._data.min_session_coverage_pct.scaleb(-2)
        return have > 0 and Decimal(have) >= need

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

    def _overnight_bars(self, minutes: Sequence[Bar], today: date) -> tuple[list[Bar], int]:
        """Bars of the overnight session that led into `today`, and its length in minutes."""
        night = self._instrument.overnight_session
        zone = ZoneInfo(night.timezone)
        start_day = today - timedelta(days=1) if night.start > night.end else today
        begin = datetime.combine(start_day, night.start, zone)
        finish = datetime.combine(today, night.end, zone) + _MINUTE
        bars = [bar for bar in minutes if begin <= bar.ts.astimezone(zone) < finish]
        return bars, _span_minutes(begin, finish)


def _span_minutes(begin: datetime, end: datetime) -> int:
    """Real minutes between two local times (converted to UTC, so DST is respected)."""
    return int((end.astimezone(UTC) - begin.astimezone(UTC)).total_seconds() // 60)
