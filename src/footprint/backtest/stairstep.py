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
  Until that close the steps still count: the bars after the formed stair are the next
  push and its pause, and if that step is invalid before the entry close (its pause
  fails (a) or (b), runs past max_pause_bars, or, for stairstep_vol, fails the volume
  test when it ends) the count is zero and there is no entry. After an entry the count
  starts again from zero.
- The stair runs from the first push's start bar to the entry bar; more than
  max_stair_bars bars and it is dropped (count zero). A step dropped this way, or by
  a pause longer than max_pause_bars, is an invalid step like any other: its bars are
  still that push and pause, so the pause runs on until a bar exceeds the push high,
  and that bar's push starts the new count (Q5.2-c). A missing bar (any step in time
  other than one bar interval: a gap, the daily halt, days with no bars) drops it too, and no
  push starts across the missing time.
- The down move is the exact mirror: every price is reflected (highs <-> lows).

Pure and stateless: the same bars give the same answer however often it is asked.

StairTrigger puts the detector in the momentum slot of the decision pipeline (Q5.2
slice 2a). It answers for the LAST bar only, and scans from the last missing bar
onward: a missing bar drops every stair and all state (`_drop()`), so the bars before
it cannot change the answer, and the scan stays one session long instead of the whole
history the engine hands each decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from footprint.core.models import Bar, Read, Score
from footprint.core.series import require_series


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
        self._invalid = False  # the step in progress is already invalid; count is zero

    def _invalidate(self) -> None:
        """The step in progress is invalid: the count is zero and there is no stair,
        but the push and its pause run on until a bar exceeds the push high."""
        self._count = 0
        self._target = None
        self._invalid = True

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

    def _already_invalid(self) -> bool:
        """(a) or (b) has failed on the pause so far. A pause low only falls as the pause
        grows, so neither can pass again by the time the pause ends."""
        assert self._push_start_low is not None
        pause_low = min(self._pause_lows)
        if self._prev_pause_low is not None and not pause_low > self._prev_pause_low:
            return True
        giveback = self._push_high - pause_low
        return giveback > self._s.max_giveback * (self._push_high - self._push_start_low)

    def step(self, i: int, bar: _View, prev: _View | None) -> None:
        """Bar i; `prev` is bar i-1 when it is exactly one interval earlier, else None."""
        if prev is None:
            self._drop()
            return
        if self._stair_start is not None and i - self._stair_start + 1 > self._s.max_stair_bars:
            self._invalidate()
        if self._push_start_low is None:  # looking for a push (never with a formed stair)
            if bar.high > prev.high:
                self._start_push(i - 1, prev, bar)
            return
        if not self._pause_lows and bar.high > self._push_high:  # the push goes on
            self._push_high = bar.high
            self._push_volumes.append(bar.volume)
        elif bar.high > self._push_high:  # the pause ends: judge the step
            if not self._invalid and self._step_valid():
                self._count += 1
                self._prev_pause_low = min(self._pause_lows)
            else:
                self._drop()
            if self._count == self._s.steps:
                self._target = self._push_high
            self._start_push(i - 1, prev, bar)
        else:  # a pause bar
            self._pause_lows.append(bar.low)
            self._pause_volumes.append(bar.volume)
            if len(self._pause_lows) > self._s.max_pause_bars or (
                self._target is not None and self._already_invalid()
            ):
                self._invalidate()
        if self._target is not None and bar.close > self._target:
            self.entries.append(i)
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


def fires_on_last(
    bars: Sequence[Bar],
    settings: StairSettings,
    interval: timedelta,
    *,
    rising: bool,
    with_volume: bool,
) -> bool:
    """True when the stair entry fires on the last bar. Equal to `len(bars) - 1 in
    stair_entries(bars, ...)`, but scans only from the last missing bar onward."""
    start = len(bars) - 1
    while start > 0 and bars[start].ts - bars[start - 1].ts == interval:
        start -= 1
    entries = stair_entries(
        bars[start:], settings, interval, rising=rising, with_volume=with_volume
    )
    return len(bars) - 1 - start in entries


class StairTrigger:
    """Q5.2 "stairstep" / "stairstep_vol" as an EntryTrigger: BULLISH when the up stair
    enters on the last bar, BEARISH when the down stair does, NEUTRAL otherwise (and
    NEUTRAL, failing closed, if both ever did on one bar)."""

    def __init__(self, settings: StairSettings, interval: timedelta, *, with_volume: bool) -> None:
        self._settings = settings
        self._interval = interval
        self._with_volume = with_volume

    @property
    def agent_id(self) -> str:
        return "momentum"

    def read(self, bars: Sequence[Bar]) -> Read:
        require_series(bars, None, "StairTrigger bars")
        last = bars[-1]
        up = fires_on_last(
            bars, self._settings, self._interval, rising=True, with_volume=self._with_volume
        )
        down = fires_on_last(
            bars, self._settings, self._interval, rising=False, with_volume=self._with_volume
        )
        name = "stairstep_vol" if self._with_volume else "stairstep"
        if up and down:
            return Read(self.agent_id, last.ts, Score.NEUTRAL, f"{name}: both directions")
        if up:
            return Read(self.agent_id, last.ts, Score.BULLISH, f"{name}: up stair entry")
        if down:
            return Read(self.agent_id, last.ts, Score.BEARISH, f"{name}: down stair entry")
        return Read(self.agent_id, last.ts, Score.NEUTRAL, f"{name}: no stair entry")
