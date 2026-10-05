"""Rafael (volume): is the latest bar a volume spike relative to its time of day?

Volume is not a vote in the console (it is already part of the momentum trigger),
so Rafael reports the spike and its candle direction: +1 a bullish spike candle,
-1 a bearish one, 0 no spike (or a spike on a doji, or not enough history).
"""

from __future__ import annotations

from collections.abc import Sequence

from footprint.agents._series import candle_direction, relative_volume, require_series
from footprint.core.config import InstrumentProfile, VolumeSettings
from footprint.core.models import Bar, Read, Score


class VolumeReader:
    def __init__(self, settings: VolumeSettings, instrument: InstrumentProfile) -> None:
        self._settings = settings
        self._timezone = instrument.prime_session.timezone

    @property
    def agent_id(self) -> str:
        return "volume"

    def read(self, bars: Sequence[Bar]) -> Read:
        require_series(bars, self._settings.timeframe_minutes, "VolumeReader bars")
        last = bars[-1]
        rel = relative_volume(bars, len(bars) - 1, self._settings.lookback_sessions, self._timezone)
        if rel is None:
            return Read(
                self.agent_id,
                last.ts,
                Score.NEUTRAL,
                f"cannot measure: needs {self._settings.lookback_sessions} same-time bars "
                "with non-zero volume",
            )
        if rel < self._settings.spike_multiple:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, f"relative volume {rel:.2f}x")
        direction = candle_direction(last)
        return Read(
            self.agent_id,
            last.ts,
            direction,
            f"volume spike {rel:.2f}x on a {direction.name.lower()} candle",
        )
