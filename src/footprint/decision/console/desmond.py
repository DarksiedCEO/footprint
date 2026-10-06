"""Desmond (console): rules only. Turns the crew's reads into a go / no-go.

Step 1, hard gates (any failure = no trade, but every gate is still evaluated so the
decision log shows the whole picture):
- the moment is in the prime session and before the close lead (G8; the overnight
  higher bar is not built in v1, so overnight never trades);
- the trend has a direction (G14: a neutral trend means no trade);
- momentum fired in the trend's direction (NB-14: no countertrend entries);
- Nia (zero-day) is not tripped;
- levels are available and show no wall before the target (a -1 from Grant);
- VWAP side (the VWAP never-break rule): longs only above VWAP, shorts only below.
Step 2, confirmer score: levels (+1 -> levels_weight) plus range (-1/0/+1 x
range_weight); fire at fire_threshold or more. Volume is not scored (it is already
inside the momentum trigger).
Step 3: the verdict carries every read, gate and score for the journal.

Desmond never picks a contract, sizes or approves risk; the pipeline does those
after a passing verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from footprint.agents.levels import LevelsReader, LevelsSnapshot, Unavailable
from footprint.agents.range_reader import RangeReader
from footprint.agents.zero_day import ZeroDayCheck
from footprint.core.calendar import SessionKind, entry_cutoff, session_at
from footprint.core.config import ConsoleSettings, InstrumentProfile
from footprint.core.models import Bar, Direction, Read, Score, require_utc


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ConsoleInputs:
    as_of: datetime
    trend: Read
    momentum: Read
    volume: Read
    zero_day: ZeroDayCheck
    levels: LevelsSnapshot | Unavailable
    range_days: Sequence[Bar] | None  # completed cash days + today; None before the cash open
    n_points: Decimal


@dataclass(frozen=True, slots=True)
class ConsoleVerdict:
    as_of: datetime
    session: SessionKind
    direction: Direction | None
    gates: tuple[Gate, ...]
    reads: tuple[Read, ...]
    score: int
    threshold: int

    @property
    def go(self) -> bool:
        return self.direction is not None and all(g.passed for g in self.gates)

    @property
    def failed(self) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates if not g.passed)


_DIRECTION = {Score.BULLISH: Direction.LONG, Score.BEARISH: Direction.SHORT}


class Console:
    def __init__(
        self,
        settings: ConsoleSettings,
        instrument: InstrumentProfile,
        levels: LevelsReader,
        range_reader: RangeReader,
    ) -> None:
        self._settings = settings
        self._instrument = instrument
        self._levels = levels
        self._range = range_reader

    @property
    def agent_id(self) -> str:
        return "console"

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        as_of = inputs.as_of
        require_utc(as_of, "as_of")
        s = self._settings
        gates: list[Gate] = []
        reads: list[Read] = [inputs.trend, inputs.momentum, inputs.volume]

        session = session_at(as_of, self._instrument)
        gates.append(Gate("prime_session", session is SessionKind.PRIME, session.value))
        zone = ZoneInfo(self._instrument.prime_session.timezone)
        local = as_of.astimezone(zone)
        cutoff = entry_cutoff(as_of, self._instrument, s.close_lead_minutes).astimezone(zone)
        gates.append(
            Gate(
                "before_close_lead",
                local < cutoff,
                f"no new entries from {cutoff.time()} ({s.close_lead_minutes} min before close)",
            )
        )

        direction = _DIRECTION.get(inputs.trend.score)
        gates.append(
            Gate("trend_has_direction", direction is not None, f"trend: {inputs.trend.reason}")
        )
        momentum = _DIRECTION.get(inputs.momentum.score)
        agrees = direction is not None and momentum is direction
        gates.append(
            Gate(
                "momentum_with_trend",
                agrees,
                f"momentum {inputs.momentum.score.name} vs trend {inputs.trend.score.name}: "
                f"{inputs.momentum.reason}",
            )
        )
        gates.append(
            Gate(
                "zero_day_clear",
                not inputs.zero_day.tripped,
                "; ".join(inputs.zero_day.findings),
            )
        )

        score = 0
        if direction is None:
            gates.append(Gate("levels_room", False, "no direction to measure room for"))
            gates.append(Gate("vwap_side", False, "no direction"))
        else:
            levels_read = self._levels.assess(inputs.levels, direction, inputs.n_points)
            reads.append(levels_read)
            gates.append(
                Gate("levels_room", levels_read.score is not Score.BEARISH, levels_read.reason)
            )
            if isinstance(inputs.levels, LevelsSnapshot):
                snap = inputs.levels
                above = snap.price > snap.vwap
                below = snap.price < snap.vwap
                side_ok = above if direction is Direction.LONG else below
                gates.append(
                    Gate(
                        "vwap_side",
                        side_ok,
                        f"{direction.value} at {snap.price} vs VWAP {snap.vwap:.2f}",
                    )
                )
            else:
                gates.append(Gate("vwap_side", False, f"no VWAP: {inputs.levels.reason}"))
            if levels_read.score is Score.BULLISH:
                score += s.levels_weight
            if inputs.range_days:
                range_read = self._range.assess(inputs.range_days, direction)
            else:
                range_read = Read("range", as_of, Score.NEUTRAL, "cash session not open yet")
            reads.append(range_read)
            score += int(range_read.score) * s.range_weight

        gates.append(
            Gate(
                "confirmer_score",
                score >= s.fire_threshold,
                f"score {score} vs threshold {s.fire_threshold}",
            )
        )
        return ConsoleVerdict(
            as_of=as_of,
            session=session,
            direction=direction,
            gates=tuple(gates),
            reads=tuple(reads),
            score=score,
            threshold=s.fire_threshold,
        )
