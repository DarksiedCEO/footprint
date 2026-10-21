"""L1: the London opening-range breakout, as pre-registered in docs/research/london-v1.md.

All clock times are config (research.yaml `rules.london_orb`), New York wall time:
- range: the high and low of the bars opening in [range_start, range_end). A session
  day missing any minute of that window is not traded (no partial range).
- entry: a bar opening in [entry_start, entry_end) whose CLOSE is above the range high
  buys a call (direction +1), below the range low buys a put (-1); a close on or
  inside the range is nothing. The entry is at that close (the engine's rule).
- setup stop: the opposite side of the range, as the signal's futures stop; the
  engine's option-dollar exit competes with it, whichever comes first.
- green stop: no new entry once the session day's realized net is at or above the
  variant's `green_stop_dollars` (the grid in config; each value is one variant). An
  open trade is never touched by the rule: it rides its own exit.
- at most `max_trades_per_session` trades per session day; one position at a time
  (the engine never asks while a trade is open); hard flat at `flat`.

No lookahead: asked at bar i, the rule reads bars 0..i only. The range is taken from
the bars before i (all of which open before the entry window, so before bar i).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from footprint.core.config import ConfigError, _check_keys, _integer
from footprint.research.bars import Day
from footprint.research.engine import DayState, Signal
from footprint.research.settings import ResearchSettings, _clock

NAME = "london_orb"
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class OrbSettings:
    range_start: time
    range_end: time
    entry_start: time
    entry_end: time
    flat: time
    max_trades_per_session: int
    green_stop_dollars: tuple[float, ...]


def load_orb(settings: ResearchSettings) -> OrbSettings:
    where = f"research.yaml: rules: {NAME}"
    if NAME not in settings.rules:
        raise ConfigError(f"research.yaml: rules: missing {NAME}")
    raw: Mapping[str, object] = _check_keys(
        settings.rules[NAME],
        {
            "range_start",
            "range_end",
            "entry_start",
            "entry_end",
            "flat",
            "max_trades_per_session",
            "green_stop_dollars",
        },
        where,
    )
    clocks = {
        key: _clock(raw, key, where)
        for key in ("range_start", "range_end", "entry_start", "entry_end", "flat")
    }
    order = ("range_start", "range_end", "entry_start", "entry_end", "flat")
    for earlier, later in pairwise(order):
        same_allowed = (earlier, later) == ("range_end", "entry_start")
        if clocks[later] < clocks[earlier] or (
            clocks[later] == clocks[earlier] and not same_allowed
        ):
            raise ConfigError(f"{where}: {later} must come after {earlier} (same morning)")
    grid = raw["green_stop_dollars"]
    if not isinstance(grid, list) or not grid:
        raise ConfigError(f"{where}: green_stop_dollars must be a non-empty list")
    values: list[float] = []
    for item in grid:
        if isinstance(item, bool) or not isinstance(item, int | float) or not item > 0:
            raise ConfigError(f"{where}: green_stop_dollars entries must be > 0, got {item!r}")
        values.append(float(item))
    if len(set(values)) != len(values):
        raise ConfigError(f"{where}: green_stop_dollars has duplicates")
    return OrbSettings(
        range_start=clocks["range_start"],
        range_end=clocks["range_end"],
        entry_start=clocks["entry_start"],
        entry_end=clocks["entry_end"],
        flat=clocks["flat"],
        max_trades_per_session=_integer(raw, "max_trades_per_session", where, minimum=1),
        green_stop_dollars=tuple(values),
    )


def _label(value: float) -> str:
    return format(value, "g")


def orb_variants(settings: ResearchSettings) -> tuple[str, ...]:
    return tuple(_label(v) for v in load_orb(settings).green_stop_dollars)


def build_orb(settings: ResearchSettings, variant: str | None) -> LondonOrb:
    orb = load_orb(settings)
    for value in orb.green_stop_dollars:
        if variant == _label(value):
            return LondonOrb(orb, value, settings.zone)
    raise ValueError(
        f"{NAME}: green stop variant {variant!r} is not pre-registered "
        f"({[_label(v) for v in orb.green_stop_dollars]})"
    )


def _minutes(start: time, end: time) -> int:
    base = date.min
    return (
        datetime.combine(base, end, tzinfo=UTC) - datetime.combine(base, start, tzinfo=UTC)
    ) // _MINUTE


class LondonOrb:
    name = NAME

    def __init__(self, settings: OrbSettings, green_stop: float, zone: ZoneInfo) -> None:
        self.settings = settings
        self.green_stop = green_stop
        self._zone = zone
        self._range_minutes = _minutes(settings.range_start, settings.range_end)

    def _range(self, day: Day, i: int) -> tuple[float, float] | None:
        s = self.settings
        inside = [k for k in range(i) if s.range_start <= day.clock[k] < s.range_end]
        if len({day.clock[k] for k in inside}) != self._range_minutes:
            return None
        return max(day.high[k] for k in inside), min(day.low[k] for k in inside)

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None:
        s = self.settings
        if not s.entry_start <= day.clock[i] < s.entry_end:
            return None
        if state.trades >= s.max_trades_per_session or state.realized >= self.green_stop:
            return None
        bounds = self._range(day, i)
        if bounds is None:
            return None
        high, low = bounds
        close = day.close[i]
        flat_by = datetime.combine(day.day, s.flat, tzinfo=self._zone)
        if close > high:
            return Signal(1, flat_by, low)
        if close < low:
            return Signal(-1, flat_by, high)
        return None
