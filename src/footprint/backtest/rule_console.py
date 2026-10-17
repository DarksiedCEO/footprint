"""The rule console (backtest only): the console for entry rules that decide on their own.

Andre's decision (2026-10-17, docs/strategy-rules.md): the Bollinger rules run with the
old confirmers OFF. This console keeps only the time gates the live console applies
first (prime session; no new entry inside the close lead) and takes the direction from
the entry rule's read. The trend, zero-day, levels, VWAP and score gates are not
applied. Everything after the console is unchanged: the contract, sizing, the full risk
check (kill switch, daily and weekly loss limits, trade ceilings), the hard stop.

Nothing outside footprint.backtest imports this module, so it cannot reach the live
path: the live runner always builds the live console.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from footprint.core.calendar import SessionKind, entry_cutoff, session_at
from footprint.core.config import ConsoleSettings, InstrumentProfile
from footprint.core.models import Direction, Score, require_utc
from footprint.decision.console.desmond import ConsoleInputs, ConsoleVerdict, Gate

_DIRECTION = {Score.BULLISH: Direction.LONG, Score.BEARISH: Direction.SHORT}


class RuleConsole:
    def __init__(self, settings: ConsoleSettings, instrument: InstrumentProfile) -> None:
        self._settings = settings
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "rule_console"

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        as_of = inputs.as_of
        require_utc(as_of, "as_of")
        s = self._settings
        session = session_at(as_of, self._instrument)
        zone = ZoneInfo(self._instrument.prime_session.timezone)
        local = as_of.astimezone(zone)
        cutoff = entry_cutoff(as_of, self._instrument, s.close_lead_minutes).astimezone(zone)
        direction = _DIRECTION.get(inputs.momentum.score)
        gates = (
            Gate("prime_session", session is SessionKind.PRIME, session.value),
            Gate(
                "before_close_lead",
                local < cutoff,
                f"no new entries from {cutoff.time()} ({s.close_lead_minutes} min before close)",
            ),
            Gate("rule_has_direction", direction is not None, inputs.momentum.reason),
        )
        return ConsoleVerdict(
            as_of=as_of,
            session=session,
            direction=direction,
            gates=gates,
            reads=(inputs.momentum, inputs.trend, inputs.volume),
            score=0,
            threshold=0,
        )
