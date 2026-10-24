"""N: the noise-band breakout, as pre-registered in docs/research/noiseband-es-v1.md.

The source is the earlier research tournament's `noise_band` entry (tournament.py, the
variant the pre-registration names), first signal of the day only. Its signals are reproduced
exactly (a parity test runs both on the same synthetic bars). All numbers and clock
times are config (research.yaml `rules.noise_band`), New York wall time:

- regular session: the bars opening in [rth_start, rth_end) of a weekday session day.
  A day is KEPT when it has at least `min_rth_bars` of them; a day that is not kept is
  never traded and never counted below (not a previous close, not history).
- session minute m of a regular bar: minutes from rth_start to its open.
- warm-up: a kept day trades only with `warmup_days` kept days before it.
- sigma at minute m: the mean, over those earlier kept days, of
  |close of that day's last regular bar opening at or before m / that day's first
  regular open - 1|; a day with no bar at or before m is left out of the mean (no
  value at all: no signal).
- bands: top = max(today's first regular open, the previous kept day's last regular
  close) * (1 + band_multiple * sigma); bottom = min(the same two) * (1 - band_multiple
  * sigma).
- checked only at the CLOSE of a regular bar with m >= first_check_minute and (m + 1)
  a multiple of check_every_minutes, opening before `flat`, that has a next regular
  bar: a close above the top buys a call, below the bottom a put; on or inside a band
  is nothing. The entry is at the NEXT bar's open (the engine's `next_open`).
- at most `max_trades_per_session` trades a day (pre-registered: the first signal
  only); hard flat at `flat`.
- fail closed where the harness cannot do what the source does (review round 1; both
  are counted in the report): NB-11, a signal whose next bar opens at or after `flat`
  (a data gap) is refused (the source would still enter on it), and the day is not
  traded; NB-12, a kept day whose last regular bar opens before `flat` is not traded
  (the source exits at that day's last regular close, the harness only at a bar from
  `flat` on, possibly after the session). NB-12 is known only at the day's end, like the
  keep rule; it only ever removes trades.
- every signal carries the day's range unit for the adr_bracket exit: the mean of the
  regular-session high minus low of the same earlier kept days.

No lookahead in prices: asked at bar i, the rule reads today's bars up to i only, and
earlier days. One thing is known only at the day's end, inherited from the source rule
and pre-registered: whether the day is kept (its regular bar count). The rule also
needs to know that bar i has a next regular bar (its time, not its price).
"""

from __future__ import annotations

import calendar
import statistics
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from footprint.core.config import ConfigError, _check_keys, _integer, _positive_decimal
from footprint.research.bars import Day
from footprint.research.engine import DayState, Signal
from footprint.research.settings import ResearchSettings, _clock

NAME = "noise_band"
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class NoiseSettings:
    rth_start: time
    rth_end: time
    min_rth_bars: int
    warmup_days: int
    band_multiple: float
    first_check_minute: int
    check_every_minutes: int
    flat: time
    max_trades_per_session: int


def load_noise(settings: ResearchSettings) -> NoiseSettings:
    where = f"research.yaml: rules: {NAME}"
    if NAME not in settings.rules:
        raise ConfigError(f"research.yaml: rules: missing {NAME}")
    raw: Mapping[str, object] = _check_keys(
        settings.rules[NAME],
        {
            "rth_start",
            "rth_end",
            "min_rth_bars",
            "warmup_days",
            "band_multiple",
            "first_check_minute",
            "check_every_minutes",
            "flat",
            "max_trades_per_session",
        },
        where,
    )
    start, end, flat = (_clock(raw, key, where) for key in ("rth_start", "rth_end", "flat"))
    if not start < end:
        raise ConfigError(f"{where}: rth_end must come after rth_start (same day)")
    if end > settings.roll:
        raise ConfigError(
            f"{where}: rth_end must not be after the session day roll "
            f"({settings.roll:%H:%M}): the regular session must sit in one session day"
        )
    if not start < flat < end:
        raise ConfigError(f"{where}: flat must fall inside the regular session")
    return NoiseSettings(
        rth_start=start,
        rth_end=end,
        min_rth_bars=_integer(raw, "min_rth_bars", where, minimum=1),
        warmup_days=_integer(raw, "warmup_days", where, minimum=1),
        band_multiple=float(_positive_decimal(raw, "band_multiple", where)),
        first_check_minute=_integer(raw, "first_check_minute", where, minimum=0),
        check_every_minutes=_integer(raw, "check_every_minutes", where, minimum=1),
        flat=flat,
        max_trades_per_session=_integer(raw, "max_trades_per_session", where, minimum=1),
    )


def noise_variants(settings: ResearchSettings) -> tuple[str, ...]:
    load_noise(settings)
    return ()


def build_noise(settings: ResearchSettings, variant: str | None) -> NoiseBand:
    if variant is not None:
        raise ValueError(f"{NAME}: has no variants, got {variant!r}")
    return NoiseBand(load_noise(settings), settings.zone)


def _minute(clock: time, start: time) -> int:
    base = date.min
    return (
        datetime.combine(base, clock, tzinfo=UTC) - datetime.combine(base, start, tzinfo=UTC)
    ) // _MINUTE


@dataclass(frozen=True, slots=True)
class _Session:
    """One session day's regular-session bars, as the rule sees them."""

    positions: tuple[int, ...]  # indexes into the Day of its regular bars, in order
    minutes: tuple[int, ...]  # their session minutes
    kept: bool
    first_open: float  # of the first regular bar (kept days only)
    last_close: float  # of the last regular bar (kept days only)
    span: float  # regular-session high minus low (kept days only)
    closes: tuple[float, ...]
    to_close: bool  # its last regular bar opens in the regular session's last minute
    ends_early: bool  # kept, and its last regular bar opens before the flat time


@dataclass(frozen=True, slots=True)
class _Context:
    session: _Session
    past: tuple[_Session, ...] | None  # the warm-up kept days before (None: not warm)
    previous_close: float | None
    range_points: float | None
    where: dict[int, int]  # Day index -> position among the regular bars


class NoiseBand:
    name = NAME

    def __init__(self, settings: NoiseSettings, zone: ZoneInfo) -> None:
        self.settings = settings
        self._zone = zone
        self._days: dict[date, _Context] = {}

    def _session(self, day: Day) -> _Session:
        s = self.settings
        positions = tuple(k for k in range(len(day)) if s.rth_start <= day.clock[k] < s.rth_end)
        weekday = day.day.weekday() < calendar.SATURDAY
        kept = weekday and len(positions) >= s.min_rth_bars
        last = _minute(day.clock[positions[-1]], s.rth_start) if positions else None
        to_close = last is not None and last + 1 >= _minute(s.rth_end, s.rth_start)
        if not kept:
            return _Session(positions, (), False, 0.0, 0.0, 0.0, (), to_close, False)
        return _Session(
            positions=positions,
            minutes=tuple(_minute(day.clock[k], s.rth_start) for k in positions),
            kept=True,
            first_open=day.open[positions[0]],
            last_close=day.close[positions[-1]],
            span=max(day.high[k] for k in positions) - min(day.low[k] for k in positions),
            closes=tuple(day.close[k] for k in positions),
            to_close=to_close,
            ends_early=day.clock[positions[-1]] < s.flat,
        )

    def prepare(self, days: Sequence[Day]) -> None:
        """Index every session day (called once by the engine before the run)."""
        w = self.settings.warmup_days
        kept: list[_Session] = []
        self._days = {}
        for day in sorted(days, key=lambda d: d.day):
            session = self._session(day)
            past = tuple(kept[-w:]) if len(kept) >= w else None
            self._days[day.day] = _Context(
                session=session,
                past=past if session.kept else None,
                previous_close=kept[-1].last_close if kept and session.kept else None,
                range_points=None
                if past is None or not session.kept
                else statistics.fmean(p.span for p in past),
                where={k: n for n, k in enumerate(session.positions)},
            )
            if session.kept:
                kept.append(session)

    def _context(self, day: Day) -> _Context:
        context = self._days.get(day.day)
        if context is None:
            raise ValueError(f"{NAME}: {day.day} was not given to prepare() before the run")
        return context

    def kept(self, day: Day) -> bool:
        return self._context(day).session.kept

    def range_points(self, day: Day) -> float | None:
        return self._context(day).range_points

    def sigma(self, day: Day, i: int) -> float | None:
        """The noise at bar i's session minute, or None (not warm, not regular, or no
        earlier kept day has a bar at or before that minute)."""
        c = self._context(day)
        n = c.where.get(i)
        if c.past is None or n is None:
            return None
        minute = c.session.minutes[n]
        values = []
        for p in c.past:
            j = bisect_right(p.minutes, minute) - 1
            if j >= 0:
                values.append(abs(p.closes[j] / p.first_open - 1))
        return statistics.fmean(values) if values else None

    def levels(self, day: Day, i: int) -> tuple[float, float] | None:
        """(top, bottom) band at bar i, or None."""
        c = self._context(day)
        sigma = self.sigma(day, i)
        if sigma is None or c.previous_close is None:
            return None
        s = sigma * self.settings.band_multiple
        first = c.session.first_open
        return (
            max(first, c.previous_close) * (1 + s),
            min(first, c.previous_close) * (1 - s),
        )

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None:
        s = self.settings
        if state.trades >= s.max_trades_per_session:
            return None
        signal = self._signal(day, i)
        if signal is None or day.clock[i + 1] >= s.flat:  # NB-11
            return None
        return signal

    def _signal(self, day: Day, i: int) -> Signal | None:
        """The source rule's signal at bar i (a next regular bar exists), before NB-11."""
        s = self.settings
        c = self._context(day)
        n = c.where.get(i)
        if n is None or c.past is None or c.session.ends_early:  # NB-12
            return None
        if n + 1 >= len(c.session.positions) or day.clock[i] >= s.flat:
            return None
        minute = c.session.minutes[n]
        if minute < s.first_check_minute or (minute + 1) % s.check_every_minutes:
            return None
        bands = self.levels(day, i)
        if bands is None:
            return None
        top, bottom = bands
        close = day.close[i]
        flat_by = datetime.combine(day.day, s.flat, tzinfo=self._zone)
        if close > top:
            return Signal(1, flat_by, next_open=True, range_points=c.range_points)
        if close < bottom:
            return Signal(-1, flat_by, next_open=True, range_points=c.range_points)
        return None

    def _refused(self, day: Day) -> bool:
        """Whether the day's first source signal was refused by NB-11."""
        for i in range(len(day)):
            if self._signal(day, i) is not None:
                return day.clock[i + 1] >= self.settings.flat
        return False

    def report(self, days: Sequence[Day]) -> list[str]:
        """Lines for the results: how many days were kept, warming up and tradable,
        and the keep-rule caveat."""
        if any(d.day not in self._days for d in days):
            self.prepare(days)
        contexts = [self._days[d.day] for d in days]
        kept = sum(1 for c in contexts if c.session.kept)
        tradable = sum(1 for c in contexts if c.past is not None)
        s = self.settings
        gaps = [
            d.day
            for d, c in zip(days, contexts, strict=True)
            if not c.session.kept and c.session.to_close and d.day.weekday() < calendar.SATURDAY
        ]
        early = [
            d.day for d, c in zip(days, contexts, strict=True) if c.past and c.session.ends_early
        ]
        refused = [d.day for d in days if self._refused(d)]
        return [
            f"session days: {len(days)}; kept: {kept}; not kept (not a weekday, or too "
            f"few regular bars): {len(days) - kept}",
            f"kept days warming up: {kept - tradable}; tradable: {tradable}",
            _listed(
                "L-3 dropped weekdays with regular bars to the close (mid-day gaps, lookahead)",
                gaps,
            ),
            _listed("NB-11 signals refused, next bar at or after the flat time", refused),
            _listed("NB-12 kept days ending before the flat time (not traded)", early),
            f"keep-rule caveat: a day is kept only with at least {s.min_rth_bars} bars "
            f"opening {s.rth_start:%H:%M}-{s.rth_end:%H:%M}, a full day's bar count known "
            "only at its end (inherited from the source rule, as pre-registered).",
            "entries are at the open of the bar after the signal bar; the last regular bar "
            "of a day never signals.",
        ]


def _listed(label: str, dates: Sequence[date]) -> str:
    return f"{label}: {len(dates)}" + ("" if not dates else ": " + ", ".join(map(str, dates)))
