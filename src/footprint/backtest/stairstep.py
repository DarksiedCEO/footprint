"""Q5.2 "stairstep" / "stairstep_vol": the stair detector (backtest rule library only).

Andre's approved definition (2026-10-10) is in docs/strategy-rules.md word for word.
How each word is read here (docs/decisions-pending.md, Q5.2-a):

- PUSH: the bar the push starts from, then a run of consecutive bars each with a high
  above the highest high so far in the push (that start bar included). The push high
  is the last new high; "the low the push started from" is the start bar's low. A push
  needs at least one new-high bar. Its average volume counts the new-high bars only.
- PAUSE: the bars after the push whose high does not exceed the push high. It ends on
  the first bar whose high does (that bar is the next push's first new-high bar; the
  last pause bar is the next push's start bar). The pause low is its lowest low.
- STEP valid: (a) the pause low is strictly above the previous valid step's pause low
  (the first step of a count has no previous step, so (a) does not apply); (b) push
  high minus pause low <= max_giveback x (push high minus the start bar's low);
  stairstep_vol only: the push's average bar volume is strictly greater than the
  pause's. A pause is judged when it ends; a pause longer than max_pause_bars is an
  invalid step at once.
- An invalid step resets the count to zero; the push that ends its pause starts a
  new count. After `steps` valid steps the stair is formed and the ENTRY is the first
  bar (from the bar that ends the last pause) whose CLOSE is above the last push high.
  After an entry the count starts again from zero.
- The stair runs from the first push's start bar to the entry bar; more than
  max_stair_bars bars and it is dropped (count zero). A missing bar (any step in time
  other than one bar interval: a gap, the daily halt, days with no bars) drops it too, and no
  push starts across the missing time.
- The down move is the exact mirror: every price is reflected (highs <-> lows).

Pure and stateless: the same bars give the same answer however often it is asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from footprint.core.models import Bar


@dataclass(frozen=True, slots=True)
class StairSettings:
    steps: int
    max_giveback: Decimal
    max_pause_bars: int
    max_stair_bars: int

    def __post_init__(self) -> None:
        if self.steps < 1:
            raise ValueError("stairstep steps must be at least one")
        if not Decimal(0) < self.max_giveback <= Decimal(1):
            raise ValueError("stairstep max_giveback must be above 0 and at most 1")
        if self.max_pause_bars < 1:
            raise ValueError("stairstep max_pause_bars must be at least one")
        if self.max_stair_bars < 1:
            raise ValueError("stairstep max_stair_bars must be at least one")


@dataclass(frozen=True, slots=True)
class _View:
    """One bar seen in the rule's direction: for a down move every price is negated,
    so "high" is the (negated) low and a new high is a new low."""

    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _view(bar: Bar, rising: bool) -> _View:
    if rising:
        return _View(bar.high, bar.low, bar.close, Decimal(bar.volume))
    return _View(-bar.low, -bar.high, -bar.close, Decimal(bar.volume))


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


class _Scan:
    """The forward state machine over one bar sequence."""

    def __init__(self, settings: StairSettings, with_volume: bool) -> None:
        self._s = settings
        self._with_volume = with_volume
        self.entries: list[int] = []
        self._drop()

    def _drop(self) -> None:
        """Count zero, nothing in progress: look for the next push."""
        self._count = 0
        self._prev_pause_low: Decimal | None = None
        self._stair_start: int | None = None
        self._push_start_low: Decimal | None = None  # None: no push in progress
        self._push_high = Decimal(0)
        self._push_volumes: list[Decimal] = []
        self._pause_lows: list[Decimal] = []
        self._pause_volumes: list[Decimal] = []
        self._target: Decimal | None = None  # set once the stair is formed

    def _start_push(self, start: int, start_bar: _View, first: _View) -> None:
        if self._count == 0:
            self._stair_start = start
        self._push_start_low = start_bar.low
        self._push_high = first.high
        self._push_volumes = [first.volume]
        self._pause_lows = []
        self._pause_volumes = []

    def _step_valid(self) -> bool:
        assert self._push_start_low is not None
        pause_low = min(self._pause_lows)
        if self._prev_pause_low is not None and not pause_low > self._prev_pause_low:
            return False
        giveback = self._push_high - pause_low
        if giveback > self._s.max_giveback * (self._push_high - self._push_start_low):
            return False
        return not (
            self._with_volume and not _mean(self._push_volumes) > _mean(self._pause_volumes)
        )

    def step(self, i: int, bar: _View, prev: _View | None) -> None:
        """Bar i; `prev` is bar i-1 when it is exactly one interval earlier, else None."""
        if prev is None:
            self._drop()
            return
        if self._stair_start is not None and i - self._stair_start + 1 > self._s.max_stair_bars:
            self._drop()
        if self._target is not None:
            if bar.close > self._target:
                self.entries.append(i)
                self._drop()
            return
        if self._push_start_low is None:  # looking for a push
            if bar.high > prev.high:
                self._start_push(i - 1, prev, bar)
            return
        if not self._pause_lows:  # in a push
            if bar.high > self._push_high:
                self._push_high = bar.high
                self._push_volumes.append(bar.volume)
            else:
                self._pause_lows.append(bar.low)
                self._pause_volumes.append(bar.volume)
            return
        if bar.high > self._push_high:  # the pause ends: judge the step
            if self._step_valid():
                self._count += 1
                self._prev_pause_low = min(self._pause_lows)
            else:
                self._drop()
            if self._count == self._s.steps:
                self._target = self._push_high
                if bar.close > self._target:
                    self.entries.append(i)
                    self._drop()
                return
            self._start_push(i - 1, prev, bar)
            return
        self._pause_lows.append(bar.low)
        self._pause_volumes.append(bar.volume)
        if len(self._pause_lows) > self._s.max_pause_bars:
            self._drop()


def stair_entries(
    bars: Sequence[Bar],
    settings: StairSettings,
    interval: timedelta,
    *,
    rising: bool,
    with_volume: bool,
) -> list[int]:
    """Indices of the bars on which the stair entry fires (oldest first)."""
    scan = _Scan(settings, with_volume)
    previous: Bar | None = None
    for i, bar in enumerate(bars):
        contiguous = previous is not None and bar.ts - previous.ts == interval
        scan.step(
            i, _view(bar, rising), _view(previous, rising) if contiguous and previous else None
        )
        previous = bar
    return scan.entries
