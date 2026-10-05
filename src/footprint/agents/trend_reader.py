"""Mateo (trend): the direction of the trend on the trend timeframe.

Uptrend (+1): the last two swing highs and the last two swing lows are both rising,
the fast EMA is above the slow EMA, and the last close is above both.
Downtrend (-1): the mirror image. Anything else, or too little data: 0.
A swing high is a bar whose high is strictly above the highs of `swing_strength`
bars on each side; a swing low mirrors it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from footprint.agents._series import ema, require_series
from footprint.core.config import TrendSettings
from footprint.core.models import Bar, Read, Score


def _swings(values: Sequence[Decimal], strength: int, *, high: bool) -> list[Decimal]:
    found = []
    for i in range(strength, len(values) - strength):
        neighbours = [*values[i - strength : i], *values[i + 1 : i + strength + 1]]
        value = values[i]
        if (high and all(value > n for n in neighbours)) or (
            not high and all(value < n for n in neighbours)
        ):
            found.append(value)
    return found


class TrendReader:
    def __init__(self, settings: TrendSettings) -> None:
        self._settings = settings

    @property
    def agent_id(self) -> str:
        return "trend"

    def read(self, bars: Sequence[Bar]) -> Read:
        s = self._settings
        require_series(bars, s.timeframe_minutes, "TrendReader bars")
        last = bars[-1]
        closes = [bar.close for bar in bars]
        fast, slow = ema(closes, s.ema_fast), ema(closes, s.ema_slow)
        if not slow:
            return Read(
                self.agent_id,
                last.ts,
                Score.NEUTRAL,
                f"insufficient bars: {len(bars)} of {s.ema_slow} for the slow EMA",
            )
        highs = _swings([bar.high for bar in bars], s.swing_strength, high=True)
        lows = _swings([bar.low for bar in bars], s.swing_strength, high=False)
        if len(highs) < 2 or len(lows) < 2:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, "fewer than two swing highs/lows")
        f, sl = fast[-1], slow[-1]
        rising = highs[-1] > highs[-2] and lows[-1] > lows[-2]
        falling = highs[-1] < highs[-2] and lows[-1] < lows[-2]
        if rising and f > sl and last.close > f:
            return Read(self.agent_id, last.ts, Score.BULLISH, "higher highs/lows above EMAs")
        if falling and f < sl and last.close < f:
            return Read(self.agent_id, last.ts, Score.BEARISH, "lower highs/lows below EMAs")
        return Read(self.agent_id, last.ts, Score.NEUTRAL, "no clean trend")
