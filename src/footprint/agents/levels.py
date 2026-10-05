"""Grant (levels): how much room a trade has before the next opposing level.

Levels tracked each session (G10): prior cash-session high/low/close, the overnight
session's high/low, VWAP anchored at the cash open, and the trend timeframe's swing
highs/lows. Round numbers are tracked and reported but are NOT walls in v1: with
the profit target needing more points than the round-number spacing, the next round
number is always closer than the target and every trade would be vetoed (Andre to
decide; see the spec-gap list).

Scoring against N, the underlying points needed for the profit target:
distance to the nearest opposing level >= room_multiple x N  -> +1 (the console
weights it); below wall_multiple x N -> -1, which the console treats as a veto;
in between -> 0. No opposing level at all means unlimited room.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from footprint.agents._series import swing_points
from footprint.core.config import InstrumentProfile, LevelsSettings, TrendSettings
from footprint.core.models import Bar, Direction, Read, Score, require_utc
from footprint.core.series import require_series
from footprint.data.aggregate import completed, session_days


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
        self, settings: LevelsSettings, trend: TrendSettings, instrument: InstrumentProfile
    ) -> None:
        self._settings = settings
        self._trend = trend
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "levels"

    def snapshot(
        self, minute_bars: Sequence[Bar], trend_bars: Sequence[Bar], as_of: datetime
    ) -> LevelsSnapshot | Unavailable:
        """Levels at `as_of` from 1-minute bars and trend-timeframe bars.

        Only bars complete at `as_of` are used. Fails closed (Unavailable) when the
        prior session, the overnight session or today's VWAP is missing.
        """
        require_utc(as_of, "as_of")
        minutes = completed(minute_bars, as_of, 1)
        if not minutes:
            return Unavailable(as_of, "no completed 1-minute bars")
        require_series(minutes, 1, "LevelsReader minute bars")
        inst = self._instrument
        cash = inst.cash_session
        zone = ZoneInfo(cash.timezone)
        today = as_of.astimezone(zone).date()

        days = session_days(minutes, cash, as_of)
        prior = [day for day in days.days if day.ts.astimezone(zone).date() < today]
        if not prior:
            return Unavailable(as_of, "no completed prior cash session")
        prev = prior[-1]

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

        overnight = self._overnight_bars(minutes, as_of)
        if not overnight:
            return Unavailable(as_of, "no bars from the overnight session before today")

        walls = [
            Level("prior_high", prev.high),
            Level("prior_low", prev.low),
            Level("prior_close", prev.close),
            Level("overnight_high", max(bar.high for bar in overnight)),
            Level("overnight_low", min(bar.low for bar in overnight)),
            Level("vwap", vwap),
        ]
        trend = completed(trend_bars, as_of, self._trend.timeframe_minutes)
        if trend:
            require_series(trend, self._trend.timeframe_minutes, "LevelsReader trend bars")
        recent = trend[-self._settings.swing_lookback_bars :]
        strength = self._trend.swing_strength
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
        return LevelsSnapshot(
            as_of=as_of,
            price=price,
            vwap=vwap,
            walls=tuple(walls),
            round_below=(price / step).to_integral_value(ROUND_FLOOR) * step,
            round_above=(price / step).to_integral_value(ROUND_CEILING) * step,
        )

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
            ahead = [(lv.price - price, lv) for lv in snapshot.walls if lv.price > price]
        else:
            ahead = [(price - lv.price, lv) for lv in snapshot.walls if lv.price < price]
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

    def _overnight_bars(self, minutes: Sequence[Bar], as_of: datetime) -> list[Bar]:
        """Bars of the overnight session that led into today's trading day."""
        night = self._instrument.overnight_session
        zone = ZoneInfo(night.timezone)
        today = as_of.astimezone(zone).date()
        if night.start > night.end:
            begin = datetime.combine(today - timedelta(days=1), night.start, zone)
        else:
            begin = datetime.combine(today, night.start, zone)
        finish = datetime.combine(today, night.end, zone) + timedelta(minutes=1)
        return [bar for bar in minutes if begin <= bar.ts.astimezone(zone) < finish]
