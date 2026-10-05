"""Diego (execution): turns ONE approved entry into a filled, protected position.

- No order without risk approval (NB-03): Diego accepts only an ApprovedEntry that
  the same pipeline issued, uses each approval once, refuses one older than the
  quote age limit, and re-runs the risk precheck and the close-lead check at send
  time.
- Limit orders only (NB-12): the order model has no market type.
- G5 entry: start at the plan's floor (mid rounded down), step step_ticks every
  step_seconds, at most max_steps, never above the plan's ceiling, cancel after
  timeout_seconds. Only the planned contract, never more than the planned size.
- Resting hard stop (NB-02): in the same call that sees the fill, Booker opens the
  stop and Diego places the resting stop-limit at the broker for the filled size.
  If the broker rejects the stop, Diego reports it so management flattens at once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from footprint.core.calendar import SessionKind, session_at
from footprint.core.config import Profiles
from footprint.core.models import Direction, require_utc
from footprint.decision.pipeline import ApprovedEntry, Pipeline
from footprint.decision.risk.gate import precheck
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper
from footprint.execution.halt import Halt
from footprint.execution.ledger import Ledger
from footprint.execution.market import QuoteBook
from footprint.execution.orders import Order, OrderBroker, OrderState, OrderType, Side

log = logging.getLogger("footprint.execution")


class EntryOutcome(Enum):
    WORKING = "WORKING"
    PROTECTED = "PROTECTED"  # filled, and the resting stop is at the broker
    UNPROTECTED = "UNPROTECTED"  # filled, but the broker refused the resting stop
    UNFILLED = "UNFILLED"  # cancelled or rejected with nothing filled


@dataclass(slots=True)
class Position:
    position_id: str
    contract_id: str
    direction: Direction
    contracts: int
    entry_price: Decimal
    entry_ts: datetime
    session: SessionKind
    stop_order_id: str | None
    exit_order_id: str | None = None
    events: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EntryTicket:
    position_id: str
    approved: ApprovedEntry
    order_id: str
    started: datetime
    limit: Decimal
    outcome: EntryOutcome = EntryOutcome.WORKING
    position: Position | None = None


class EntryRefused(RuntimeError):
    """Diego will not send this entry. The message says why."""


class Executor:
    def __init__(
        self,
        profiles: Profiles,
        pipeline: Pipeline,
        broker: OrderBroker,
        stops: StopKeeper,
        ledger: Ledger,
        halt: Halt,
        book: QuoteBook,
    ) -> None:
        self._p = profiles
        self._halt = halt
        self._book = book
        self._pipeline = pipeline
        self._broker = broker
        self._stops = stops
        self._ledger = ledger
        self._claimed: set[int] = set()
        self._sequence = 0

    @property
    def agent_id(self) -> str:
        return "execution"

    def submit(
        self, approved: ApprovedEntry, blackouts: Iterable[NewsBlackout], now: datetime
    ) -> EntryTicket:
        require_utc(now, "now")
        p = self._p
        if self._halt.tripped:
            raise EntryRefused("halted: " + "; ".join(self._halt.reasons))
        if not isinstance(approved, ApprovedEntry) or not self._pipeline.issued(approved):
            raise EntryRefused("not an approval issued by this pipeline")
        if id(approved) in self._claimed:
            raise EntryRefused("this approval was already used")
        self._claimed.add(id(approved))  # claimed before anything can be sent
        max_age = timedelta(seconds=p.execution.quotes.max_age_seconds)
        if now - approved.entry.ts > max_age or now < approved.entry.ts:
            raise EntryRefused("approval is stale")
        if now - approved.plan.choice.chain_ts > max_age:
            raise EntryRefused("the option quote behind the approval is stale")
        decided = approved.plan.choice.quote
        live = self._book.latest(decided.contract_id)
        if live is None or now - live.ts > max_age or live.ts > now:
            raise EntryRefused("no fresh quote in the book for the chosen contract")
        if (live.bid, live.ask) != (decided.bid, decided.ask):
            raise EntryRefused(
                f"the market moved since the decision: book {live.bid}/{live.ask} vs "
                f"decided {decided.bid}/{decided.ask}"
            )
        pre = precheck(p.risk, p.instrument, blackouts, self._ledger.account(now), now)
        if not pre.allowed:
            raise EntryRefused(
                "risk precheck at send time: " + ", ".join(v.value for v in pre.vetoes)
            )
        prime = p.instrument.prime_session
        zone = ZoneInfo(prime.timezone)
        local = now.astimezone(zone)
        cutoff = datetime.combine(local.date(), prime.end, zone) - timedelta(
            minutes=p.strategy.console.close_lead_minutes
        )
        if session_at(now, p.instrument) is not SessionKind.PRIME or local >= cutoff:
            raise EntryRefused("outside the entry window at send time")

        self._sequence += 1
        position_id = f"{now:%Y%m%dT%H%M%S}-{self._sequence}"
        plan = approved.plan
        order = Order(
            order_id=f"{position_id}-entry",
            contract_id=plan.choice.quote.contract_id,
            side=Side.BUY,
            quantity=plan.contracts,
            order_type=OrderType.LIMIT,
            limit_price=plan.limit_floor,
        )
        ticket = EntryTicket(position_id, approved, order.order_id, now, plan.limit_floor)
        status = self._broker.place(order)
        log.info("entry %s placed at %s", order.order_id, plan.limit_floor)
        return self._after(ticket, status.state, status.filled, status.avg_price, now)

    def abandon(self, ticket: EntryTicket) -> None:
        """Cancel a working entry (reconcile takes over whatever it may have filled)."""
        try:
            self._broker.cancel(ticket.order_id)
        except Exception as exc:  # reconcile will see and cancel it from the broker's list
            log.critical("could not cancel entry %s: %r", ticket.order_id, exc)

    def on_time(self, ticket: EntryTicket, now: datetime) -> EntryTicket:
        """Advance a working entry: fill check, price steps, timeout."""
        require_utc(now, "now")
        if ticket.outcome is not EntryOutcome.WORKING:
            return ticket
        status = self._broker.status(ticket.order_id)
        if status.state is not OrderState.WORKING:
            return self._after(ticket, status.state, status.filled, status.avg_price, now)
        e = self._p.execution.entry
        elapsed = now - ticket.started
        if elapsed >= timedelta(seconds=e.timeout_seconds):
            self._broker.cancel(ticket.order_id)
            status = self._broker.status(ticket.order_id)  # the broker's word, not the reply
            return self._after(ticket, status.state, status.filled, status.avg_price, now)
        steps = min(e.max_steps, int(elapsed.total_seconds() // e.step_seconds))
        tick = self._p.instrument.option_tick_size
        plan = ticket.approved.plan
        target = min(plan.limit_floor + tick * e.step_ticks * steps, plan.limit_ceiling)
        if target > ticket.limit:
            self._broker.modify(ticket.order_id, target, None)
            status = self._broker.status(ticket.order_id)
            ticket.limit = status.order.limit_price
            if status.state is not OrderState.WORKING:
                return self._after(ticket, status.state, status.filled, status.avg_price, now)
        return ticket

    def _after(
        self,
        ticket: EntryTicket,
        state: OrderState,
        filled: int,
        price: Decimal | None,
        now: datetime,
    ) -> EntryTicket:
        if state is OrderState.WORKING:
            return ticket
        if filled < 1 or price is None:
            ticket.outcome = EntryOutcome.UNFILLED
            log.info("entry %s ended unfilled (%s)", ticket.order_id, state.value)
            return ticket
        plan = ticket.approved.plan
        session = session_at(now, self._p.instrument)
        protected = False
        stop: Order | None = None
        try:
            self._stops.open(ticket.position_id, price, filled)
            spec = self._stops.resting_order(ticket.position_id)
            stop = Order(
                order_id=f"{ticket.position_id}-stop",
                contract_id=plan.choice.quote.contract_id,
                side=Side.SELL,
                quantity=filled,
                order_type=OrderType.STOP_LIMIT,
                limit_price=spec.limit,
                stop_price=spec.trigger,
            )
            stop_status = self._broker.place(stop)
            protected = stop_status.state in (OrderState.WORKING, OrderState.FILLED)
        except Exception as exc:  # any failure here: find out what the broker really has
            log.critical("resting stop for %s uncertain: %r", ticket.position_id, exc)
            stop_id = f"{ticket.position_id}-stop"
            try:
                held = self._broker.status(stop_id)  # did it reach the broker after all?
                if held.state in (OrderState.WORKING, OrderState.FILLED):
                    stop, protected = held.order, True  # adopt it (a fill is handled next)
            except Exception as again:  # unknown to the broker, or cannot ask: unprotected
                log.critical("cannot confirm the stop %s: %r", stop_id, again)
        try:
            self._ledger.on_entry_filled(now, ticket.position_id, session)
        except Exception as exc:  # the broker holds the stop; nothing new may be sent
            self._halt.trip(f"ledger did not record {ticket.position_id}: {exc!r}")
        ticket.position = Position(
            position_id=ticket.position_id,
            contract_id=plan.choice.quote.contract_id,
            direction=ticket.approved.signal.direction,
            contracts=filled,
            entry_price=price,
            entry_ts=now,
            session=session,
            stop_order_id=stop.order_id if protected and stop is not None else None,
        )
        ticket.outcome = EntryOutcome.PROTECTED if protected else EntryOutcome.UNPROTECTED
        if not protected:
            log.critical("broker refused the resting stop for %s: flatten now", ticket.position_id)
        return ticket
