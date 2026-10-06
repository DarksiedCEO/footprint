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
Every alarm and halt is passed to `on_alarm`, which must reach Andre. Live, that is an
`AlarmLine` (execution/alarms.py), so a slow or broken pager never delays a step.

Failing safe:
- a news calendar that cannot be read is treated as a blackout in force now: the
  position is flattened and no entry is approved until it reads again;
- an open position that has not been managed for `max_unmanaged_seconds` (inputs
  keep failing, steps keep crashing) is flattened through the broker and the quote
  book alone (`Manager.flatten`), on every step until it is closed;
- while the alarm line is down, at most one delivery is tried per step, after the
  step's safety work; undelivered alarms wait in a queue without duplicates.

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

from footprint.core.config import RunnerSettings
from footprint.core.models import Bar, Direction, require_utc
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
from footprint.execution.management.malik import Alarm, Closed, ExitReason, Manager
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
        settings: RunnerSettings,
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
        self._channel_down = False
        self._tried_channel = False
        self._failure_kinds: set[str] = set()
        self._max_unmanaged = timedelta(seconds=settings.max_unmanaged_seconds)
        self._managed_at: datetime | None = None
        self._failsafe_alarmed = False
        self._reconcile_flattening = False  # the last reconcile disagreed: it is acting
        # One line to Andre: management alarms, halts and latches all come through here.
        manager.route_alarms(self._deliver)
        halt.route(lambda message: self._alarm(self._now, message))
        self._reconciling = True
        self._ticket: EntryTicket | None = None
        self._position: Position | None = None
        self._birth: Decision | None = None
        self._last_decided: datetime | None = ledger.last_decided  # survives a restart
        self._latched = False
        self.closed: list[Closed] = []
        self._alarm(started, "STARTED: reconciling; no entries until Andre clears to trade")
        if self._last_decided is not None and self._last_decided > started:
            self._alarm(
                started,
                f"the last decided minute ({self._last_decided.isoformat()}) is in the future: "
                "no decision will be made until then. Check the clock",
            )
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

    @property
    def last_decided(self) -> datetime | None:
        """The newest completed bar a decision has been made on."""
        return self._last_decided

    def close_out(self, now: datetime) -> bool:
        """Backtest only: the history has ended. Cancel a working entry and sell an open
        position through the normal exit path (at the bid), so the trade is booked and
        journaled like any other. True if nothing is left open."""
        require_utc(now, "now")
        if self._ticket is not None:
            self._executor.abandon(self._ticket)
            self._ticket = None
        if self._position is not None:
            position = self._position
            closed = self._manager.flatten(position, now, ExitReason.END_OF_DATA)
            if closed is not None:
                self._trade_done(position, closed, now)
        return self._position is None

    @property
    def exposure(self) -> Direction | None:
        """The direction of the open position, or of the entry still working; None
        when there is neither."""
        if self._position is not None:
            return self._position.direction
        if self._ticket is not None:
            return self._ticket.approved.signal.direction
        return None

    def doubt(self) -> None:
        """Something does not add up: reconcile before doing anything else."""
        self._reconciling = True

    def step(self, now: datetime) -> None:
        """One pass. Never raises: any failure alarms, halts and sends the runner to
        reconcile, so a broken collaborator can never leave a position unwatched.
        """
        require_utc(now, "now")
        self._now = now
        self._tried_channel = False
        try:
            self._book.mark_time(now)
            self._step(now)
            self._watch_latch(now)
        except Exception as exc:
            kind = type(exc).__name__
            self._halt.trip(f"step failed: {kind}")  # safety first, then the alarm
            self.doubt()
            if kind not in self._failure_kinds:  # once per kind: the halt already holds
                self._failure_kinds.add(kind)
                self._alarm(now, f"step failed: {exc!r}; halting and reconciling")
            else:
                log.exception("step failed again")
        self._failsafe(now)
        self._retry_alarms()

    def _step(self, now: datetime) -> None:
        if self._reconciling:
            self._reconcile(now)
            return
        blackouts = self._safe_blackouts(now)

        if self._ticket is not None:
            ticket = self._executor.on_time(self._ticket, now)
            self._ticket = ticket if ticket.outcome is EntryOutcome.WORKING else None
            if ticket.outcome is not EntryOutcome.WORKING:
                self._entry_done(ticket, now)

        if self._position is not None:
            self._manage(now, blackouts)
            return

        if self._ticket is None:
            self._decide(now, blackouts)

    def _reconcile(self, now: datetime) -> None:
        if self._ticket is not None:
            # A working entry must not outlive the doubt: cancel it; reconcile handles
            # whatever it may have filled.
            self._executor.abandon(self._ticket)
            self._ticket = None
        # Pessimistic: until reconcile says it agrees, assume it may have a sell out
        # (it can raise after its order reached the broker).
        self._reconcile_flattening = True
        agreed = self._manager.reconcile(self._position, now)
        self._reconcile_flattening = not agreed
        for closed in self._manager.take_reconcile_closes():
            if self._position is not None and closed.position_id == self._position.position_id:
                self._trade_done(self._position, closed, now)
        if agreed:
            self._reconciling = False
        elif self._position is not None and self._ledger.open_position_id is None:
            self._position, self._managed_at = None, None

    def _entry_done(self, ticket: EntryTicket, now: datetime) -> None:
        """The one path for every finished entry, however it finished."""
        self._position = ticket.position
        if self._position is not None:
            self._managed_at = now  # the unmanaged clock starts at the fill
        if ticket.outcome is EntryOutcome.UNPROTECTED:
            self._alarm(now, f"{ticket.position_id}: filled without a resting stop")
        if self._position is None:
            self._birth = None

    def _trade_done(self, position: Position, closed: Closed, now: datetime) -> None:
        self.closed.append(closed)
        birth = self._birth
        self._position, self._birth, self._managed_at = None, None, None
        self._failsafe_alarmed = False
        self.doubt()  # confirm the broker is really flat before the next decision
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
        try:
            # Durable before the decision: a crash inside it (or in the send it leads
            # to) can never make a restart decide this minute again.
            self._ledger.mark_decided(newest)
        except Exception as exc:
            self._halt.trip("cannot save the decided minute")
            if "decided" not in self._failure_kinds:
                self._failure_kinds.add("decided")
                self._alarm(now, f"cannot save the decided minute: {exc!r}; not deciding")
            return
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
                self._manage(now, blackouts)

    def _manage(self, now: datetime, blackouts: tuple[NewsBlackout, ...]) -> None:
        assert self._position is not None
        closed = self._manager.manage(self._position, blackouts, now)
        self._managed_at = now
        if closed is not None:
            self._trade_done(self._position, closed, now)

    def _safe_blackouts(self, now: datetime) -> tuple[NewsBlackout, ...]:
        """A calendar that cannot be read is a blackout in force now (fail safe)."""
        try:
            return tuple(self._blackouts(now))
        except Exception as exc:
            if "calendar" not in self._failure_kinds:
                self._failure_kinds.add("calendar")
                self._alarm(now, f"news calendar unreadable: {exc!r}; treating as a blackout")
            return (NewsBlackout("news calendar unreadable", now, now),)

    def _failsafe(self, now: datetime) -> None:
        """Flatten a position that normal management has not reached for too long.
        Stands aside only while reconcile is flattening on its own (its last round
        disagreed): two sellers could sell the contract twice. Merely being sent back
        to reconcile, where it agrees, never disarms the failsafe."""
        if self._position is None or self._managed_at is None or self._reconcile_flattening:
            return
        if now - self._managed_at <= self._max_unmanaged:
            return
        position = self._position
        if not self._failsafe_alarmed:
            self._failsafe_alarmed = True
            self._alarm(
                now,
                f"{position.position_id}: unmanaged since {self._managed_at}; "
                "flattening through the broker",
            )
        try:
            closed = self._manager.flatten(position, now)
            if closed is not None:
                self._trade_done(position, closed, now)
        except Exception as exc:
            self._halt.trip(f"failsafe flatten failed: {type(exc).__name__}")
            self.doubt()
            log.exception("failsafe flatten failed")

    def _watch_latch(self, now: datetime) -> None:
        latched = self._ledger.account(now).kill_switch_latched
        if latched and not self._latched:
            self._alarm(now, "KILL SWITCH LATCHED: no entries until Andre clears it")
        self._latched = latched

    def _deliver(self, alarm: Alarm) -> None:
        """Never raises. While the line is down, alarms are queued without being tried."""
        log.critical(alarm.message)
        if self._channel_down:
            self._queue(alarm)
            return
        if not self._send(alarm):
            self._queue(alarm)

    def _send(self, alarm: Alarm) -> bool:
        self._tried_channel = True
        try:
            self._on_alarm(alarm)
        except Exception:
            log.exception("alarm channel failed; will retry")
            self._channel_down = True
            return False
        self._channel_down = False
        return True

    def _queue(self, alarm: Alarm) -> None:
        if all(queued.message != alarm.message for queued in self._undelivered):
            self._undelivered.append(alarm)

    def _retry_alarms(self) -> None:
        """After the step's safety work: one try if the line is down and nothing was
        tried this step; once it answers, send everything that waited, in order."""
        if not self._undelivered or (self._channel_down and self._tried_channel):
            return
        while self._undelivered:
            if not self._send(self._undelivered[0]):
                return  # still down: it stays first in line
            self._undelivered.pop(0)

    def _alarm(self, now: datetime, message: str) -> None:
        self._deliver(Alarm(now, message))
