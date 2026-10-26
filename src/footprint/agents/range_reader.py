"""Nadia (range): today's range against the average daily range.

A confirmer, not a direction: +1 means the day still has room to run, -1 means
it has already travelled further than an average day. `assess` adds the
direction-aware chase rule: on a day already past the quiet band, entering in the
outer chase zone of today's range in the trade's direction also scores -1.
Daily bars are cash-session bars (G11).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from footprint.agents._series import require_series
from footprint.core.config import RangeSettings
from footprint.core.models import Bar, Direction, Read, Score


class RangeReader:
    """Reads daily bars: the last bar is today (possibly incomplete), the ones before it
    are completed days. Daily bars are only checked for order, not spacing, because
    session-stamped daily bars shift by an hour across daylight-saving changes.
    """

    def __init__(self, settings: RangeSettings) -> None:
        self._settings = settings

    @property
    def agent_id(self) -> str:
        return "range"

    def read(self, bars: Sequence[Bar]) -> Read:
        read, _ = self._read(bars)
        return read

    def assess(self, bars: Sequence[Bar], direction: Direction) -> Read:
        """`read`, plus the chase rule for an entry in `direction` at today's close."""
        read, ratio = self._read(bars)
        if ratio is None or read.score is Score.BEARISH:
            return read
        if ratio < self._settings.quiet_below_pct.scaleb(-2):
            return read
        today = bars[-1]
        zone = (today.high - today.low) * self._settings.chase_zone_pct.scaleb(-2)
        chasing = (
            today.close >= today.high - zone
            if direction is Direction.LONG
            else today.close <= today.low + zone
        )
        if not chasing:
            return read
        return Read(
            self.agent_id,
            read.ts,
            Score.BEARISH,
            f"chasing: {direction.value.lower()} entry in the outer "
            f"{self._settings.chase_zone_pct}% of a range at {ratio:.1%} of ADR",
        )

    def _read(self, bars: Sequence[Bar]) -> tuple[Read, Decimal | None]:
        require_series(bars, None, "RangeReader bars")
        today = bars[-1]
        days = self._settings.adr_days
        history = bars[-days - 1 : -1]
        if len(history) < days:
            return Read(
                self.agent_id,
                today.ts,
                Score.NEUTRAL,
                f"insufficient history: {len(history)} of {days} completed days",
            ), None
        adr = sum((bar.high - bar.low for bar in history), Decimal(0)) / Decimal(days)
        if adr == 0:
            return Read(self.agent_id, today.ts, Score.NEUTRAL, f"{days}-day ADR is zero"), None
        ratio = (today.high - today.low) / adr
        if ratio < self._settings.quiet_below_pct.scaleb(-2):
            score = Score.BULLISH
        elif ratio > self._settings.extended_above_pct.scaleb(-2):
            score = Score.BEARISH
        else:
            score = Score.NEUTRAL
        return Read(
            self.agent_id, today.ts, score, f"today's range is {ratio:.1%} of the {days}-day ADR"
        ), ratio
