"""Q5.3 "pullback": the pullback detector (backtest rule library only).

Andre's approved definition (2026-10-10) is in docs/strategy-rules.md word for word.
How each word is read here (docs/decisions-pending.md, Q5.3-a; for Andre to confirm):

- The setup starts on a bar the spike test marks in the trade direction (the same
  spike test "breakout" uses; handed in as one Score per bar). Its level is the spike
  candle's HIGH (the down move: its LOW).
- Touch (a): a LATER bar's low at or below the level. The spike bar never touches
  itself.
- Hold (b): from the bar after the spike to the entry, a bar that CLOSES below the
  level cancels the setup (a close exactly at the level holds). The check comes first
  on every bar, so a touch bar or an entry candidate that closes below cancels.
- Entry (c): the first bar AFTER the touch bar (the touch bar itself never enters)
  that closes up (close > open) and above the prior bar's high. Then the setup is done.
- Lifetime: a bar more than `lifetime` after the spike bar can neither touch nor enter
  (at exactly `lifetime` it still can); the setup is dead.
- Every spike is its own setup; several can be alive at once. A bar is an entry if any
  live setup enters on it.
- A missing bar (any step in time other than one bar interval) drops every live setup:
  "the prior bar" does not exist across it.
- The down move is the exact mirror (highs <-> lows, above <-> below, up-close <->
  down-close).

Pure and stateless: the same bars give the same answer however often it is asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from footprint.agents._series import candle_direction, relative_volume
from footprint.core.config import VolumeSettings
from footprint.core.models import Bar, Read, Score
from footprint.core.series import require_series


@dataclass(frozen=True, slots=True)
class PullbackSettings:
    """The pullback rule's number (config/backtest.yaml `rules: pullback:`): how many
    minutes after the spike the setup stays alive (DEFAULT, accepted by Andre
    2026-10-10 for backtest only)."""

    lifetime_minutes: int

    def __post_init__(self) -> None:
        if self.lifetime_minutes < 1:
            raise ValueError("pullback lifetime_minutes must be at least one")

    @property
    def lifetime(self) -> timedelta:
        return timedelta(minutes=self.lifetime_minutes)


@dataclass(slots=True)
class _Setup:
    spike: int
    level: Decimal
    touched: bool = False


def _closes_beyond(bar: Bar, level: Decimal, rising: bool) -> bool:
    return bar.close < level if rising else bar.close > level


def _touches(bar: Bar, level: Decimal, rising: bool) -> bool:
    return bar.low <= level if rising else bar.high >= level


def _turns(bar: Bar, prior: Bar, rising: bool) -> bool:
    if rising:
        return bar.close > bar.open and bar.close > prior.high
    return bar.close < bar.open and bar.close < prior.low


def pullback_entries(
    bars: Sequence[Bar],
    spikes: Sequence[Score],
    lifetime: timedelta,
    interval: timedelta,
    *,
    rising: bool,
) -> list[int]:
    """Indices of the bars on which the pullback entry fires (oldest first).
    `spikes[i]` is the spike test's answer for bars[i]."""
    if len(spikes) != len(bars):
        raise ValueError("pullback needs one spike read per bar")
    wanted = Score.BULLISH if rising else Score.BEARISH
    live: list[_Setup] = []
    entries: list[int] = []
    for i, bar in enumerate(bars):
        if i and bar.ts - bars[i - 1].ts != interval:
            live = []
        fired = False
        kept: list[_Setup] = []
        for setup in live:
            if bar.ts - bars[setup.spike].ts > lifetime:
                continue
            if _closes_beyond(bar, setup.level, rising):
                continue
            if setup.touched:
                if _turns(bar, bars[i - 1], rising):
                    fired = True
                    continue
            elif _touches(bar, setup.level, rising):
                setup.touched = True
            kept.append(setup)
        live = kept
        if fired:
            entries.append(i)
        if spikes[i] is wanted:
            live.append(_Setup(i, bar.high if rising else bar.low))
    return entries


def spike_score(bars: Sequence[Bar], index: int, volume: VolumeSettings, timezone: str) -> Score:
    """The spike test "breakout" uses, on bars[index] alone (MomentumReader's first
    step, the same read VolumeReader gives for that bar): relative volume against the
    same time of day on earlier sessions at or above the configured multiple, on a
    candle with a direction. NEUTRAL when it cannot be measured, is below the multiple,
    or is a doji. Breakout's follow-through is not part of it (reading Q5.3-d)."""
    rel = relative_volume(bars, index, volume.lookback_sessions, timezone)
    if rel is None or rel < volume.spike_multiple:
        return Score.NEUTRAL
    return candle_direction(bars[index])


class PullbackTrigger:
    """Q5.3 "pullback" as an EntryTrigger: BULLISH when the up pullback enters on the
    last bar, BEARISH when the down one does, NEUTRAL otherwise. Both cannot happen on
    one bar (an up entry closes above its open, a down entry below).

    It scans back from the last bar only while bars are one interval apart and within
    the lifetime of the last bar: a setup spiked earlier is dead on the last bar, and a
    missing bar drops every setup, so the answer equals a scan of the whole history.
    Relative volume is still measured against the whole history handed in."""

    def __init__(
        self,
        settings: PullbackSettings,
        volume: VolumeSettings,
        timezone: str,
        interval: timedelta,
    ) -> None:
        self._lifetime = settings.lifetime
        self._volume = volume
        self._timezone = timezone
        self._interval = interval

    @property
    def agent_id(self) -> str:
        return "momentum"

    def read(self, bars: Sequence[Bar]) -> Read:
        require_series(bars, None, "PullbackTrigger bars")
        last = bars[-1]
        start = len(bars) - 1
        while (
            start > 0
            and bars[start].ts - bars[start - 1].ts == self._interval
            and last.ts - bars[start - 1].ts <= self._lifetime
        ):
            start -= 1
        window = bars[start:]
        spikes = [
            spike_score(bars, start + k, self._volume, self._timezone) for k in range(len(window))
        ]
        target = len(window) - 1
        if target in pullback_entries(window, spikes, self._lifetime, self._interval, rising=True):
            return Read(self.agent_id, last.ts, Score.BULLISH, "pullback: up pullback entry")
        if target in pullback_entries(window, spikes, self._lifetime, self._interval, rising=False):
            return Read(self.agent_id, last.ts, Score.BEARISH, "pullback: down pullback entry")
        return Read(self.agent_id, last.ts, Score.NEUTRAL, "pullback: no pullback entry")
