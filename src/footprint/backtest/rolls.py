"""No new entry on the first session after a contract roll (backtest only; decision BH-9).

A raw (unadjusted) front-month history switches from one contract to the next at a
session open, and the price jumps by the difference between them (BH-9 in
docs/decisions-pending.md has the measured jumps). The readers' look-back then mixes
two contracts: prior-day levels, the VWAP side and the multi-session averages are off
by that jump, enough to flip the levels room. With `rolls.block_first_session` the backtest makes no
new entry on the trading day whose session is the first on a new contract (an
overnight session belongs to the trading day it leads into): RollConsole adds a failed
gate there, journalled like any other. Positions are never held across the roll
itself (v1 is flat before the prime close and does not trade overnight).

Which sessions those are is a fact of the data, not of the instrument: the run is
given them (`--rolls FILE`, a CSV with a `session` header and one date per line, in
order; or `--rolls none` for bars with no roll). A run that blocks and was given
nothing is refused (fail closed). The live trading path never imports this module.
"""

from __future__ import annotations

import calendar
import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

from footprint.backtest.price_level import live_console
from footprint.core.calendar import trading_day_at
from footprint.core.config import InstrumentProfile, Profiles
from footprint.decision.console.desmond import ConsoleInputs, ConsoleVerdict, Gate
from footprint.decision.pipeline import Gatekeeper

NONE = "none"  # the --rolls value that declares bars with no contract roll
_HEADER = ["session"]


def load_roll_sessions(source: str) -> frozenset[date]:
    """The roll sessions named by `source`: NONE, or the path of a roll CSV."""
    if source == NONE:
        return frozenset()
    path = Path(source)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"rolls: cannot read {path}: {exc}") from exc
    rows = list(csv.reader(lines))
    if not rows or rows[0] != _HEADER:
        raise ValueError(f"rolls: {path} must start with the header line 'session'")
    found: list[date] = []
    for index, row in enumerate(rows):
        if not index:
            continue  # the header
        number = index + 1  # the file's line number
        try:
            (text,) = row
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"rolls: {path} line {number}: not a date: {row!r}") from exc
        if day.weekday() >= calendar.SATURDAY:
            raise ValueError(f"rolls: {path} line {number}: {day} is not a weekday")
        if day in found:
            raise ValueError(f"rolls: {path} line {number}: {day} listed twice")
        if found and day < found[-1]:
            raise ValueError(f"rolls: {path} line {number}: {day} is out of order")
        found.append(day)
    return frozenset(found)


class RollConsole:
    """The wrapped console's verdict, with a failed `roll_session` gate added on a roll
    session's trading day."""

    def __init__(
        self, inner: Gatekeeper, sessions: frozenset[date], instrument: InstrumentProfile
    ) -> None:
        self.inner = inner
        self._sessions = sessions
        self._instrument = instrument

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        verdict = self.inner.evaluate(inputs)
        day = trading_day_at(inputs.as_of, self._instrument)
        if day is None or day not in self._sessions:
            return verdict
        gate = Gate(
            "roll_session",
            False,
            f"{day} is the first session on a new contract: the look-back spans the "
            "contract roll (BH-9)",
        )
        return replace(verdict, gates=(*verdict.gates, gate))


def roll_console(
    console: Gatekeeper | None, profiles: Profiles, sessions: frozenset[date]
) -> Gatekeeper | None:
    """`console` itself when no roll session is blocked; otherwise it (or, when None,
    the live console) wrapped in a RollConsole."""
    if not sessions:
        return console
    inner = live_console(profiles) if console is None else console
    return RollConsole(inner, sessions, profiles.instrument)
