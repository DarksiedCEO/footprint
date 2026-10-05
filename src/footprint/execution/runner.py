"""The runner: drives every agent on a clock, one step at a time.

The runner owns no timing of its own. A driver calls `step(now)` (every second
live, or on a simulated clock in a backtest); the runner then, in order:
1. if it is reconciling (start-up, or after any doubt), reconciles until the
   broker, the ledger and its records agree, and does nothing else;
2. advances a working entry (fill, price steps, timeout); a fill becomes the open
   position, unprotected fills are managed (flattened) on the same step;
3. manages the open position on the shared quote book; a close is journaled;
4. with nothing open or working, and at most once per newly completed minute bar,
   asks the pipeline for a decision, journals it, latches the kill switch if the
   decision says so, and submits an approved entry.
Every alarm and halt is passed to `on_alarm`, which must reach Andre.

After a restart, build a new Runner (fresh halt): it starts in reconcile with no
position (`reconcile(None)`), so anything the broker still holds is flattened and
the kill switch latched; entries resume only after Andre clears it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from footprint.core.models import Bar, require_utc
from footprint.core.options import OptionChain
from footprint.decision.pipeline import Decision, Pipeline
from footprint.decision.session import NewsBlackout
from footprint.execution.executor.diego import (
    EntryOutcome,
    EntryRefused,
    EntryTicket,
    Executor,
    Position,
)
from footprint.execution.halt import Halt
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger
from footprint.execution.management.malik import Alarm, Closed, Manager

log = logging.getLogger("footprint.runner")

_MINUTE = timedelta(minutes=1)


class Runner:
    def __init__(
        self,
        pipeline: Pipeline,
        executor: Executor,
        manager: Manager,
        ledger: Ledger,
        journal: Journal,
        halt: Halt,
        bars: Callable[[datetime], Sequence[Bar]],
        chain: Callable[[datetime], OptionChain | None],
        blackouts: Callable[[datetime], Iterable[NewsBlackout]],
        on_alarm: Callable[[Alarm], None],
    ) -> None:
        self._pipeline = pipeline
        self._executor = executor
        self._manager = manager
        self._ledger = ledger
        self._journal = journal
        self._halt = halt
        self._bars = bars
        self._chain = chain
        self._blackouts = blackouts
        self._on_alarm = on_alarm
        self._reconciling = True
        self._ticket: EntryTicket | None = None
        self._position: Position | None = None
        self._birth: Decision | None = None
        self._last_decided: datetime | None = None
        self.closed: list[Closed] = []

    @property
    def position(self) -> Position | None:
        return self._position

    @property
    def reconciling(self) -> bool:
        return self._reconciling

    def doubt(self) -> None:
        """Something does not add up: reconcile before doing anything else."""
        self._reconciling = True

    def step(self, now: datetime) -> None:
        require_utc(now, "now")
        if self._reconciling:
            if self._manager.reconcile(self._position, now):
                self._reconciling = False
            elif self._position is not None and self._ledger.open_position_id is None:
                self._position = None  # reconcile booked or flattened it
            return
        blackouts = tuple(self._blackouts(now))

        if self._ticket is not None:
            self._ticket = self._executor.on_time(self._ticket, now)
            if self._ticket.outcome is not EntryOutcome.WORKING:
                self._position = self._ticket.position
                if self._ticket.outcome is EntryOutcome.UNPROTECTED:
                    self._alarm(now, f"{self._ticket.position_id}: filled without a stop")
                self._ticket = None

        if self._position is not None:
            try:
                closed = self._manager.manage(self._position, blackouts, now)
            except Exception as exc:  # never crash the loop; reconcile instead
                self._alarm(now, f"management failed: {exc!r}; reconciling")
                self._halt.trip(f"management failed: {exc!r}")
                self.doubt()
                return
            if closed is not None:
                if self._birth is not None:
                    self._journal.record_trade(self._position, closed, self._birth)
                self.closed.append(closed)
                self._position, self._birth = None, None
            return

        if self._ticket is not None:
            return
        self._decide(now, blackouts)

    def _decide(self, now: datetime, blackouts: tuple[NewsBlackout, ...]) -> None:
        bars = self._bars(now)
        closed_bars = [b for b in bars if b.ts + _MINUTE <= now]
        if not closed_bars:
            return
        newest = closed_bars[-1].ts
        if self._last_decided is not None and newest <= self._last_decided:
            return  # decide once per newly completed minute
        self._last_decided = newest
        decision = self._pipeline.decide(
            bars, self._chain(now), self._ledger.account(now), blackouts, now
        )
        self._journal.record_decision(decision)
        if decision.latch_kill_switch:
            self._ledger.latch_kill_switch("drawdown limit reached")
        if decision.approved is None:
            return
        try:
            self._ticket = self._executor.submit(decision.approved, blackouts, now)
        except EntryRefused as exc:
            log.warning("entry refused at send: %s", exc)
            return
        self._birth = decision
        if self._ticket.outcome is not EntryOutcome.WORKING:
            self._position = self._ticket.position
            self._ticket = None

    def _alarm(self, now: datetime, message: str) -> None:
        log.critical(message)
        self._on_alarm(Alarm(now, message))
