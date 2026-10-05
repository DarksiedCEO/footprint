"""Darius (momentum): the two-step entry trigger on the entry timeframe.

Step 1, the spike: the second-to-last bar is a volume spike (relative to its time
of day) on a candle with a direction. Step 2, the follow-through: the last bar
closes beyond the spike bar's close in that same direction. Both, in that order,
or no trigger. Darius never checks the trend; the console enforces trend agreement.
"""

from __future__ import annotations

from collections.abc import Sequence

from footprint.agents._series import candle_direction, relative_volume, require_series
from footprint.core.config import InstrumentProfile, VolumeSettings
from footprint.core.models import Bar, Read, Score


class MomentumReader:
    def __init__(self, settings: VolumeSettings, instrument: InstrumentProfile) -> None:
        self._settings = settings
        self._timezone = instrument.prime_session.timezone

    @property
    def agent_id(self) -> str:
        return "momentum"

    def read(self, bars: Sequence[Bar]) -> Read:
        require_series(bars, self._settings.timeframe_minutes, "MomentumReader bars")
        last = bars[-1]
        if len(bars) < 2:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, "needs a spike bar and a next bar")
        spike = bars[-2]
        rel = relative_volume(bars, len(bars) - 2, self._settings.lookback_sessions, self._timezone)
        if rel is None:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, "cannot measure spike volume")
        if rel < self._settings.spike_multiple:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, f"no spike ({rel:.2f}x)")
        direction = candle_direction(spike)
        if direction is Score.NEUTRAL:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, f"spike {rel:.2f}x on a doji")
        followed = (
            last.close > spike.close if direction is Score.BULLISH else last.close < spike.close
        )
        if not followed:
            return Read(
                self.agent_id, last.ts, Score.NEUTRAL, f"spike {rel:.2f}x, no follow-through"
            )
        return Read(
            self.agent_id,
            last.ts,
            direction,
            f"{direction.name.lower()} spike {rel:.2f}x with follow-through",
        )
