"""Nadia (range): today's range against the average daily range.

A confirmer, not a direction: +1 means the day still has room to run, -1 means
it has already travelled further than an average day.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from footprint.agents._series import require_series
from footprint.core.config import RangeSettings
from footprint.core.models import Bar, Read, Score


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
            )
        adr = sum((bar.high - bar.low for bar in history), Decimal(0)) / Decimal(days)
        if adr == 0:
            return Read(self.agent_id, today.ts, Score.NEUTRAL, f"{days}-day ADR is zero")
        ratio = (today.high - today.low) / adr
        if ratio < self._settings.quiet_below_pct.scaleb(-2):
            score = Score.BULLISH
        elif ratio > self._settings.extended_above_pct.scaleb(-2):
            score = Score.BEARISH
        else:
            score = Score.NEUTRAL
        return Read(
            self.agent_id, today.ts, score, f"today's range is {ratio:.1%} of the {days}-day ADR"
        )
