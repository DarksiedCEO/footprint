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

Every start is treated as a restart: a new Runner starts in reconcile with no
position (`reconcile(None)`), so anything the broker still holds is flattened, and
it sends no entry until Andre clears it to trade by name (`clear_to_trade`). This
start-up gate is separate from the kill switch, so a restart never resets the
drawdown peak. Every alarm, halt and latch goes to the one `on_alarm` line to
Andre; an alarm that cannot be delivered is retried and never blocks a safety step.
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
from footprint.execution.market import QuoteBook

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
        started: datetime,
        book: QuoteBook,
    ) -> None:
        """Start (or restart) the system. It starts reconciling and sends no entry
        until Andre clears it to trade by name (`clear_to_trade`).
        """
        require_utc(started, "started")
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
        self._now = started
        self._book = book
        self._cleared_by: str | None = None
        self._undelivered: list[Alarm] = []
        # One line to Andre: management alarms, halts and latches all come through here.
        manager.route_alarms(self._deliver)
        halt.route(lambda message: self._alarm(self._now, message))
        self._reconciling = True
        self._ticket: EntryTicket | None = None
        self._position: Position | None = None
        self._birth: Decision | None = None
        self._last_decided: datetime | None = None
        self._latched = False
        self.closed: list[Closed] = []
        self._alarm(started, "STARTED: reconciling; no entries until Andre clears to trade")
        self._watch_latch(started)

    def clear_to_trade(self, by: str) -> None:
        """Andre only: allow entries after a start or restart."""
        if not by.strip():
            raise ValueError("clearing to trade needs the name of who cleared it")
        self._cleared_by = by
        self._alarm(self._now, f"cleared to trade by {by}")

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
        """One pass. Never raises: any failure alarms, halts and sends the runner to
        reconcile, so a broken collaborator can never leave a position unwatched.
        """
        require_utc(now, "now")
        self._now = now
        self._retry_alarms()
        try:
            self._book.mark_time(now)
            self._step(now)
            self._watch_latch(now)
        except Exception as exc:
            self._halt.trip(f"step failed: {exc!r}")  # safety first, then the alarm
            self.doubt()
            self._alarm(now, f"step failed: {exc!r}; halting and reconciling")

    def _step(self, now: datetime) -> None:
        if self._reconciling:
            self._reconcile(now)
            return
        blackouts = tuple(self._blackouts(now))

        if self._ticket is not None:
            ticket = self._executor.on_time(self._ticket, now)
            self._ticket = ticket if ticket.outcome is EntryOutcome.WORKING else None
            if ticket.outcome is not EntryOutcome.WORKING:
                self._entry_done(ticket, now)

        if self._position is not None:
            closed = self._manager.manage(self._position, blackouts, now)
            if closed is not None:
                self._trade_done(self._position, closed, now)
            return

        if self._ticket is None:
            self._decide(now, blackouts)

    def _reconcile(self, now: datetime) -> None:
        if self._ticket is not None:
            # A working entry must not outlive the doubt: cancel it; reconcile handles
            # whatever it may have filled.
            self._executor.abandon(self._ticket)
            self._ticket = None
        agreed = self._manager.reconcile(self._position, now)
        for closed in self._manager.take_reconcile_closes():
            if self._position is not None and closed.position_id == self._position.position_id:
                self._trade_done(self._position, closed, now)
        if agreed:
            self._reconciling = False
        elif self._position is not None and self._ledger.open_position_id is None:
            self._position = None

    def _entry_done(self, ticket: EntryTicket, now: datetime) -> None:
        """The one path for every finished entry, however it finished."""
        self._position = ticket.position
        if ticket.outcome is EntryOutcome.UNPROTECTED:
            self._alarm(now, f"{ticket.position_id}: filled without a resting stop")
        if self._position is None:
            self._birth = None

    def _trade_done(self, position: Position, closed: Closed, now: datetime) -> None:
        self.closed.append(closed)
        birth = self._birth
        self._position, self._birth = None, None
        if birth is not None:
            try:
                self._journal.record_trade(position, closed, birth)
            except Exception as exc:  # the trade is closed; losing its record is an alarm
                self._alarm(now, f"journal failed for {closed.position_id}: {exc!r}")

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
        if decision.latch_kill_switch:
            self._ledger.latch_kill_switch("drawdown limit reached")
        try:
            self._journal.record_decision(decision)
        except Exception as exc:
            self._alarm(now, f"journal failed for a decision: {exc!r}")
        if decision.approved is None:
            return
        if self._cleared_by is None:
            log.warning("approved entry not sent: not cleared to trade since the start")
            return
        try:
            ticket = self._executor.submit(decision.approved, blackouts, now)
        except EntryRefused as exc:
            log.warning("entry refused at send: %s", exc)
            return
        self._birth = decision
        if ticket.outcome is EntryOutcome.WORKING:
            self._ticket = ticket
        else:
            self._entry_done(ticket, now)
            if self._position is not None:
                closed = self._manager.manage(self._position, blackouts, now)
                if closed is not None:
                    self._trade_done(self._position, closed, now)

    def _watch_latch(self, now: datetime) -> None:
        latched = self._ledger.account(now).kill_switch_latched
        if latched and not self._latched:
            self._alarm(now, "KILL SWITCH LATCHED: no entries until Andre clears it")
        self._latched = latched

    def _deliver(self, alarm: Alarm) -> None:
        """Never raises: an undeliverable alarm is kept and retried every step."""
        log.critical(alarm.message)
        try:
            self._on_alarm(alarm)
        except Exception:
            log.exception("alarm channel failed; will retry")
            self._undelivered.append(alarm)

    def _retry_alarms(self) -> None:
        pending, self._undelivered = self._undelivered, []
        for alarm in pending:
            self._deliver(alarm)

    def _alarm(self, now: datetime, message: str) -> None:
        self._deliver(Alarm(now, message))
