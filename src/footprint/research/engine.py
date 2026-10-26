"""Runs one rule over session days and values every trade with Black-76, minute by minute.

The rule (research/rules/) sees a session day and the index of the bar that has just
CLOSED, plus the day's state so far (trades taken, realized dollars). It must look at
bars up to that index only (each rule's tests check that it does). A signal enters at
that bar's close: futures price = the close, time = the bar's open + one minute. A
signal with `next_open` enters instead at the OPEN of the next bar of the session day
(futures price = that open, time = that bar's open); the entry bar's own extremes and
close are then walked as the first path points. No next bar: no trade.

A rule may have a `prepare(days)` method: it is called once with every session day
before the run (a rule that needs earlier days, e.g. a range average). Such a rule must
still use, for a day, only what is known before each bar it is asked about (its tests).

Pricing the path. Each later bar is walked as four points: its open (valued at the bar's
open time, and a jump from the previous close: a level it opens through fills at the
open's value), then the extreme AGAINST the trade, then the one for it, then the close
(all three valued at the end of the minute: the most time decay a long option can have
by then). Adverse first, as the backtest path model (backtest/path.py): a bar that
reaches both the stop and a profit level stops out. A level crossed between points of
one bar fills at the level.

Exits: the option-dollar exit (research/exits.py), the rule's futures stop if it gives
one (as the option's value at that futures price, at that moment), the `adr_bracket`
exit's futures stop and target (entry -/+ a configured fraction of the signal's
`range_points`, valued the same way; a signal without a positive range cannot use that
exit and is an error), and the rule's hard
flat time: the first bar opening at or after it closes everything at its open. A day
that ends first closes everything at its last close ("end_of_day"). A signal whose entry
time is at or after its own flat time is refused.

One position at a time: the rule is not asked while a trade is open, nor on the bar a
trade closes in. Nothing is ever sold short and nothing is sold twice.

Volatility: a session day's value comes from research/vol.py (no lookahead). A day with
none is not valued: its first signal is counted as skipped and the day ends there (any
later trade that day would depend on the one that could not be valued).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from footprint.research.bars import Day
from footprint.research.exits import BRACKET, Fill, OptionExit
from footprint.research.pricing import Choice, choose, value
from footprint.research.sessions import classify
from footprint.research.settings import ResearchSettings
from footprint.research.vol import VolIndex

_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class DayState:
    trades: int  # trades entered so far this session day
    realized: float  # their net dollars (closed trades only; none is open when asked)


@dataclass(frozen=True, slots=True)
class Signal:
    direction: int  # +1 buys a call, -1 buys a put
    flat_by: datetime  # hard flat time (aware)
    futures_stop: float | None = None  # futures price that closes the trade
    next_open: bool = False  # enter at the next bar's open, not this bar's close
    range_points: float | None = None  # the rule's daily range unit (adr_bracket exit)


class Rule(Protocol):
    name: str

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None: ...


@dataclass(frozen=True, slots=True)
class Trade:
    day: date
    session: str  # session of the entry time (the trigger bar's close)
    direction: int
    entry_ts: datetime
    exit_ts: datetime  # when the last contract was sold
    contracts: int
    entry_value: float  # option points, at the reference price level
    net: float  # dollars, all contracts, costs included
    reasons: tuple[str, ...]  # one per fill, in order
    # Open profit (net dollars, all contracts) at entry and at every path point valued,
    # ending at `net`: the curve marked inside the trade (37-M3).
    marks: tuple[float, ...] = ()


def _walk(
    day: Day,
    start: int,
    choice: Choice,
    signal: Signal,
    vol: float,
    exit_: OptionExit,
    settings: ResearchSettings,
    marks: list[float],
    levels: tuple[float, float] | None = None,
) -> tuple[list[Fill], datetime, int]:
    """Fills, the time of the last one, and the index of the bar it happened in. Appends
    the marked open profit at every path point with no fill to `marks`. `levels`: the
    adr_bracket exit's (stop, target) futures prices. With `signal.next_open` the trade
    entered at the open of bar start + 1, so that bar is walked from its extremes on."""
    opt = settings.option
    fills: list[Fill] = []

    def at(futures: float, ts: datetime, gap: bool) -> None:
        level = None
        if signal.futures_stop is not None:
            level = value(choice, signal.futures_stop, ts, vol, opt)
        bracket = None
        if levels is not None:
            bracket = (
                value(choice, levels[0], ts, vol, opt),
                value(choice, levels[1], ts, vol, opt),
            )
        now = value(choice, futures, ts, vol, opt)
        held = exit_.open
        fills.extend(exit_.check(now, gap=gap, futures_level=level, bracket=bracket))
        if exit_.open == held:  # no fill here: mark at this point
            marks.append(exit_.marked(now))

    for j in range(start + 1, len(day)):
        opened = day.ts[j]
        entered_here = signal.next_open and j == start + 1
        if not entered_here and opened >= signal.flat_by:
            fills.extend(exit_.flat(value(choice, day.open[j], opened, vol, opt), "flat"))
            return fills, opened, j
        end = opened + _MINUTE
        against, then = (
            (day.low[j], day.high[j]) if choice.direction == 1 else (day.high[j], day.low[j])
        )
        if not entered_here:
            at(day.open[j], opened, True)
            if exit_.done:
                return fills, opened, j
        for futures in (against, then, day.close[j]):
            at(futures, end, False)
            if exit_.done:
                return fills, end, j
    last = len(day) - 1
    end = day.ts[last] + _MINUTE
    fills.extend(exit_.flat(value(choice, day.close[last], end, vol, opt), "end_of_day"))
    return fills, end, last


def _bracket_levels(
    name: str, signal: Signal, price: float, settings: ResearchSettings
) -> tuple[float, float]:
    """The adr_bracket exit's (stop, target) futures prices for an entry at `price`."""
    span = signal.range_points
    bracket = settings.exits.adr_bracket
    if span is None or not span > 0:
        raise ValueError(f"{name}: the {BRACKET} exit needs a positive range, got {span!r}")
    if bracket is None:
        raise ValueError(f"{BRACKET}: no settings for this exit")
    d = signal.direction
    return (
        price - d * bracket.stop_range_fraction * span,
        price + d * bracket.target_range_fraction * span,
    )


def run(
    days: Sequence[Day], rule: Rule, vols: VolIndex, settings: ResearchSettings, exit_kind: str
) -> tuple[list[Trade], int]:
    """(trades, skipped): every trade the rule takes, valued; signals not valued."""
    trades: list[Trade] = []
    skipped = 0
    opt = settings.option
    prepare = getattr(rule, "prepare", None)
    if callable(prepare):
        prepare(days)
    for day in days:
        taken = 0
        realized = 0.0
        i = 0
        while i < len(day):
            signal = rule.entry(day, i, DayState(taken, realized))
            if signal is None:
                i += 1
                continue
            if signal.next_open:
                if i + 1 >= len(day):
                    i += 1
                    continue
                entry_ts, price = day.ts[i + 1], day.open[i + 1]
            else:
                entry_ts, price = day.ts[i] + _MINUTE, day.close[i]
            if entry_ts >= signal.flat_by:
                i += 1
                continue
            vol = vols.lookup(day.day)
            if vol is None:
                skipped += 1
                break
            choice = choose(signal.direction, price, entry_ts, opt)
            if (
                signal.futures_stop is not None
                and signal.direction * (price - signal.futures_stop) <= 0
            ):
                raise ValueError(
                    f"{rule.name}: futures stop {signal.futures_stop} is not on the losing "
                    f"side of the entry {price} (direction {signal.direction})"
                )
            levels = None
            if exit_kind == BRACKET:
                levels = _bracket_levels(rule.name, signal, price, settings)
            entry_value = value(choice, price, entry_ts, vol, opt)
            exit_ = OptionExit(
                exit_kind,
                entry_value,
                settings.exits,
                point_value=opt.point_value,
                cost=settings.round_trip_per_contract,
            )
            marks = [exit_.marked(entry_value)]
            fills, exit_ts, closed_in = _walk(
                day, i, choice, signal, vol, exit_, settings, marks, levels
            )
            net = exit_.net(fills)
            marks.append(net)
            trades.append(
                Trade(
                    day=day.day,
                    session=classify(entry_ts.astimezone(settings.zone).time(), settings.sessions),
                    direction=signal.direction,
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    contracts=exit_.contracts,
                    entry_value=entry_value,
                    net=net,
                    reasons=tuple(f.reason for f in fills),
                    marks=tuple(marks),
                )
            )
            taken += 1
            realized += net
            i = closed_in + 1
    return trades, skipped
