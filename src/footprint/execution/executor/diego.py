"""Diego (execution): turns ONE approved entry into a filled, protected position.

- No order without risk approval (NB-03): Diego accepts only an ApprovedEntry that
  the same pipeline issued, uses each approval once, refuses one older than the
  quote age limit, and re-runs the risk precheck and the close-lead check at send
  time.
- Limit orders only (NB-12): the order model has no market type.
- G5 entry: start at the plan's floor (mid rounded down), step step_ticks every
  step_seconds, at most max_steps, never above the plan's ceiling, cancel after
  timeout_seconds. Only the planned contract, never more than the planned size.
- Resting hard stop (NB-02), attached to the entry (H-2): the entry goes to the
  broker with its stop-limit attached, priced from the entry's limit (the highest it
  can fill at), and the broker activates the stop the moment the entry fills, even
  if we are disconnected then. Each price step raises the stop first, then the
  entry. When the fill is seen, Booker opens the stop at the real fill and the
  attached stop is moved to it. If the attached stop is not working by then, a new
  one is placed; if the broker rejects that too, management flattens at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from footprint.core.calendar import SessionKind, entry_cutoff, session_at
from footprint.core.config import Profiles
from footprint.core.models import Direction, require_utc
from footprint.decision.pipeline import ApprovedEntry, Pipeline
from footprint.decision.risk.gate import precheck
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper, StopOrderSpec
from footprint.execution.halt import Halt
from footprint.execution.ledger import Ledger
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    BrokerDown,
    FillUnpriced,
    NeverPlaced,
    NotSent,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    TrackedBroker,
)

log = logging.getLogger("footprint.execution")
# The one stop placed while an entry waits for its price, if its attached stop is gone
# (N3-5). Distinct from "-stop" (attached) and "-stop-2" (placed on the fill).
_PENDING_STOP_SUFFIX = "-stop-wait"


def _attached_stop_id(position_id: str) -> str:
    """The stop that goes out attached to the entry."""
    return f"{position_id}-stop"


def _fill_stop_id(position_id: str) -> str:
    """The stop placed on the fill when the attached one was not working."""
    return f"{_attached_stop_id(position_id)}-2"


def _wait_stop_id(position_id: str) -> str:
    """The stop placed while the entry's fill price was pending (N3-5)."""
    return f"{position_id}{_PENDING_STOP_SUFFIX}"


def stop_order_ids(position_id: str) -> tuple[str, ...]:
    """Every id a position's stop can have: attached to the entry, placed on the fill
    (when the attached one was not working), and placed while the entry's price was
    pending. Whatever asks whether a stop already sold asks about all of them (grind
    review G6-5). The executor builds its stop ids from the same helpers, so the list
    cannot drift from what is sent (grind review G16-3)."""
    return (_attached_stop_id(position_id), _fill_stop_id(position_id), _wait_stop_id(position_id))


def _partly_sold(stop_id: str, held: OrderStatus) -> str:
    """The halt reason for a stop that sold part of the position: ended, or still live
    (WORKING), and then not trusted as the position's protection (grind review G7-2)."""
    live = " (still live: not trusted as protection)" if held.state is OrderState.WORKING else ""
    return (
        f"stop {stop_id} sold {held.filled} of {held.order.quantity} and is "
        f"{held.state.value}{live}: nothing is placed beside it; reconcile, and check and "
        "flatten by hand"
    )


def _replacement_outcome(placed: OrderStatus) -> str:
    """How the `-stop-wait` replacement stands at the broker, for its D41 alarm. Every
    wording says what Andre must NOT do by hand where a hand stop could sell twice (a
    short): a replacement that filled has sold the position; one still live (held back,
    cancel pending) can still sell; one that sold part has sold (PR #53 review F2)."""
    if placed.protects:
        return "placed"
    if placed.state is OrderState.FILLED:
        return (
            f"placed and has already filled (sold {placed.filled}): the position is sold; "
            "do not place a stop by hand"
        )
    if placed.state is OrderState.WORKING:
        why = "held back" if placed.held_back else "being cancelled"
        return (
            f"sent, but it is live and {why} at the broker: not protecting, and it can "
            "still sell; do not place another stop beside it"
        )
    if placed.filled > 0:
        return (
            f"sent, but it sold {placed.filled} and is {placed.state.value} at the broker: "
            "do not place a stop by hand; reconcile, and check the account"
        )
    return (
        f"sent, but the broker reports it {placed.state.value}: no stop rests at the broker for it"
    )


class EntryOutcome(Enum):
    WORKING = "WORKING"
    PROTECTED = "PROTECTED"  # filled, and the resting stop is at the broker
    UNPROTECTED = "UNPROTECTED"  # filled, but the broker refused the resting stop
    UNFILLED = "UNFILLED"  # cancelled or rejected with nothing filled
    # (A fill whose price is not reported yet stays WORKING: see Executor._price_pending.)


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
    stop_order_id: str | None = None  # the stop attached to the entry
    # When the broker first reported a fill of this entry with no price (N-1).
    price_pending_since: datetime | None = None


class EntryRefused(RuntimeError):
    """Diego will not send this entry. The message says why."""


class Executor:
    @property
    def broker_ready(self) -> bool:
        """Whether the broker will answer now (not disconnected, not resyncing)."""
        return self._broker.ready

    def __init__(
        self,
        profiles: Profiles,
        pipeline: Pipeline,
        broker: TrackedBroker,
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
        # Andre's alarm line (the runner routes it); None: log only.
        self._alarm_sink: Callable[[str], None] | None = None
        # Positions already alarmed for a stop replaced during the price wait (D41).
        self._wait_stop_alarmed: set[str] = set()

    def route_alarms(self, sink: Callable[[str], None]) -> None:
        """Send the executor's alarms to `sink` (the runner's one line to Andre, as
        Halt.route). A later route replaces the earlier one."""
        self._alarm_sink = sink

    def _alarm_wait_stop(self, pid: str, gone: str, state: OrderState, outcome: str) -> None:
        """D41 (b), signed by Andre 2026-10-25: ONE alarm per position whose stop
        ended at the broker (nothing sold) while the entry waited for its price and was
        replaced. Never raises: the alarm must not stop the protection it announces."""
        if pid in self._wait_stop_alarmed:
            return
        self._wait_stop_alarmed.add(pid)
        message = (
            f"{pid}: stop {gone} was {state.value.lower()} at the broker during the price "
            f"wait; a new stop {_wait_stop_id(pid)} was {outcome}"
        )
        log.critical(message)
        if self._alarm_sink is None:
            return
        try:
            self._alarm_sink(message)
        except Exception:
            log.exception("could not deliver an alarm")

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
        cutoff = entry_cutoff(now, p.instrument, p.strategy.console.close_lead_minutes)
        if session_at(now, p.instrument) is not SessionKind.PRIME or now >= cutoff:
            raise EntryRefused("outside the entry window at send time")

        self._sequence += 1
        # Unique across restarts too (the sequence restarts at 1): a new position can
        # never share its order ids with a previous run's.
        position_id = f"{now:%Y%m%dT%H%M%S}-{self._sequence}-{uuid4().hex[:8]}"
        plan = approved.plan
        try:
            spec = self._stops.preview(plan.limit_floor, plan.contracts)
        except ValueError as exc:
            raise EntryRefused(f"no hard stop can be attached: {exc}") from None
        order = Order(
            order_id=f"{position_id}-entry",
            contract_id=plan.choice.quote.contract_id,
            side=Side.BUY,
            quantity=plan.contracts,
            order_type=OrderType.LIMIT,
            limit_price=plan.limit_floor,
        )
        stop = Order(
            order_id=_attached_stop_id(position_id),
            contract_id=order.contract_id,
            side=Side.SELL,
            quantity=plan.contracts,
            order_type=OrderType.STOP_LIMIT,
            limit_price=spec.limit,
            stop_price=spec.trigger,
            oca_group=position_id,
            parent_id=order.order_id,
        )
        ticket = EntryTicket(
            position_id,
            approved,
            order.order_id,
            now,
            plan.limit_floor,
            stop_order_id=stop.order_id,
        )
        status, stop_status = self._broker.place_attached(order, stop)
        log.info("entry %s placed at %s, stop attached", order.order_id, plan.limit_floor)
        if status.state is OrderState.WORKING and not stop_status.protects:
            status = self._unprotected_entry(ticket, stop_status.state)
        return self._after(ticket, status.state, status.filled, status.avg_price, now)

    def _unprotected_entry(self, ticket: EntryTicket, stop_state: OrderState) -> OrderStatus:
        """The entry is working but its attached stop is not (refused, cancelled): an
        entry must never be able to fill without its stop at the broker (H-2), so the
        entry is cancelled. The broker's word on the entry after the cancel is
        returned (it may have filled meanwhile: the fill path then places a stop)."""
        log.critical(
            "attached stop %s is %s while entry %s works: cancelling the entry",
            ticket.stop_order_id,
            stop_state.value,
            ticket.order_id,
        )
        self._broker.cancel(ticket.order_id)
        return self._broker.status(ticket.order_id)

    def abandon(self, ticket: EntryTicket) -> None:
        """Cancel a working entry (reconcile takes over whatever it may have filled)."""
        try:
            self._broker.cancel(ticket.order_id)
        except Exception as exc:  # reconcile will see and cancel it from the broker's list
            log.critical("could not cancel entry %s: %r", ticket.order_id, exc)

    def settle(self, ticket: EntryTicket, now: datetime) -> EntryTicket:
        """Reconcile is about to run while this entry is working: cancel it if the
        broker still has it working, then take the broker's word on it (delta-3 review,
        N3-1). An entry the broker reports FILLED is never forgotten:
        - filled with its price: it is the position, protected like any fill;
        - filled, its price pending: still WORKING with `price_pending_since` set (past
          `fill_price_wait_seconds`, FillUnpriced). Reconcile must not run over it: its
          attached stop is the position's protection;
        - nothing filled: UNFILLED, its attached stop ended.
        Still WORKING with no fill known (the cancel is not confirmed): the caller drops
        it and reconcile finds and cancels it from the broker's list, as before.
        A status question that fails is re-raised, whatever is known about the entry
        (grind review G-1): with no answer the system cannot know it did not fill, so
        it is never dropped and nothing is sent on a guess. The caller keeps the ticket
        (BrokerDown: wait for the broker; anything else: a failed step) and settles it
        again on the next step that gets an answer."""
        require_utc(now, "now")
        if ticket.outcome is not EntryOutcome.WORKING:
            return ticket
        status = self._broker.status(ticket.order_id)
        if status.state is OrderState.WORKING:
            self.abandon(ticket)
            status = self._broker.status(ticket.order_id)
        return self._after(ticket, status.state, status.filled, status.avg_price, now)

    def on_time(self, ticket: EntryTicket, now: datetime) -> EntryTicket:
        """Advance a working entry: fill check, price steps, timeout."""
        require_utc(now, "now")
        if ticket.outcome is not EntryOutcome.WORKING:
            return ticket
        status = self._broker.status(ticket.order_id)
        if status.state is not OrderState.WORKING:
            return self._after(ticket, status.state, status.filled, status.avg_price, now)
        if ticket.stop_order_id is not None:
            # Every step: the attached stop must still be working (H-2).
            stop_status = self._broker.status(ticket.stop_order_id)
            if not stop_status.protects:
                status = self._unprotected_entry(ticket, stop_status.state)
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
            if ticket.stop_order_id is not None:
                # The stop first: it must follow the highest price the entry can fill at.
                spec = self._stops.preview(target, plan.contracts)
                self._broker.modify(ticket.stop_order_id, spec.limit, spec.trigger)
            self._broker.modify(ticket.order_id, target, None)
            status = self._broker.status(ticket.order_id)
            ticket.limit = status.order.limit_price
            if status.state is not OrderState.WORKING:
                return self._after(ticket, status.state, status.filled, status.avg_price, now)
        return ticket

    def _adopt_attached(
        self, stop_id: str, spec: StopOrderSpec, filled: int
    ) -> tuple[Order | None, bool]:
        """The attached stop, moved to the stop at the real fill: (order, protected).
        Not protected, with no order (a new stop is placed), when it is finished at the
        broker, or is for another size than was filled. Not protected WITH the order
        when it is still live but cannot be relied on (held back, or its cancel is
        pending): no second stop may go beside it (it can still fill, AEGIS K-2), so
        management gets out through its cancel instead."""
        held = self._broker.status(stop_id)
        if held.state is OrderState.FILLED:
            return held.order, True  # already triggered and filled: management books it
        if held.state is OrderState.WORKING and not held.protects:
            log.critical(
                "attached stop %s is still live but %s: no second stop beside it; flattening",
                stop_id,
                "held back" if held.held_back else "being cancelled",
            )
            return held.order, False
        if held.filled > 0:
            # It sold part of the position: ended since (cancelled by hand or by IBKR),
            # or still WORKING. A new stop for the whole fill would sell what is already
            # sold, a short (grind review G6-1). A live one is not trusted either: moving
            # it, or the size check below (cancel, then a new stop for the whole fill),
            # would act on a stop that already sold (G7-2, decided fail-closed). Kept as
            # the position's stop, untouched, so management gets out through its cancel
            # and hands the sale to reconcile.
            self._halt.trip(_partly_sold(stop_id, held))
            return held.order, False
        if not held.protects:
            log.critical("attached stop %s is %s: placing a new one", stop_id, held.state.value)
            return None, False
        if held.order.quantity != filled:
            log.critical(
                "attached stop %s is for %s, %s filled: cancelling it, placing a new one",
                stop_id,
                held.order.quantity,
                filled,
            )
            self._broker.cancel(stop_id)
            after = self._broker.status(stop_id)
            if after.state is not OrderState.CANCELLED:
                # Not confirmed gone: still able to sell, so nothing beside it (L2-6).
                log.critical("attached stop %s did not cancel (%s)", stop_id, after.state.value)
                return after.order, False
            return None, False
        if (held.order.stop_price, held.order.limit_price) != (spec.trigger, spec.limit):
            held = self._broker.modify(stop_id, spec.limit, spec.trigger)
        if held.protects or held.state is OrderState.FILLED:
            return held.order, True
        if held.state is OrderState.WORKING:  # live, not protecting: nothing beside it
            log.critical("attached stop %s cannot be relied on after the move", stop_id)
            return held.order, False
        log.critical(
            "attached stop %s is %s after the move: placing a new one", stop_id, held.state.value
        )
        return None, False

    def _end_attached(self, ticket: EntryTicket) -> None:
        """An unfilled entry takes its attached stop with it at the broker; confirm it
        (and cancel it if it is somehow still working). Never raises: a stop left
        recorded as working is found and cancelled by the next reconcile."""
        if ticket.stop_order_id is None:
            return
        try:
            if self._broker.status(ticket.stop_order_id).state is OrderState.WORKING:
                log.critical(
                    "attached stop %s outlived its entry: cancelling", ticket.stop_order_id
                )
                self._broker.cancel(ticket.stop_order_id)
        except Exception as exc:
            log.critical("cannot confirm attached stop %s ended: %r", ticket.stop_order_id, exc)

    def _price_pending(self, ticket: EntryTicket, filled: int, now: datetime) -> EntryTicket:
        """Filled at the broker, its price not reported yet (IBKR's status can run ahead
        of its execution report; delta review 2, N-1). It is a fill, never "unfilled":
        its attached stop is the position's protection and stays as it is (checked
        every step: _guard_pending_stop), and nothing is booked until the real price
        arrives (the ticket stays WORKING and is asked again every step). Past
        `fill_price_wait_seconds`: FillUnpriced, so the runner halts and reconciles
        what the broker holds. The wait is decided even when the stop check raises
        (grind review G7-3): a check that keeps failing must not hide CHECK THE FILL.
        BrokerDown is the exception: the runner waits for the broker, and the first
        step that reaches it decides."""
        if ticket.price_pending_since is None:
            ticket.price_pending_since = now
            log.warning(
                "entry %s filled %s at the broker; its price is not reported yet",
                ticket.order_id,
                filled,
            )
        since = ticket.price_pending_since
        wait = timedelta(seconds=self._p.execution.reconcile.fill_price_wait_seconds)
        overdue = FillUnpriced(
            f"entry {ticket.order_id} filled {filled} at the broker but its price has "
            f"not been reported since {since.isoformat()}"
        )
        try:
            self._guard_pending_stop(ticket, filled)
        except BrokerDown:
            raise
        except Exception as exc:
            if now - since >= wait:
                log.critical("stop check failed past the price wait: %r", exc)
                raise overdue from exc
            raise
        if now - since >= wait:
            raise overdue
        return ticket

    def _guard_pending_stop(self, ticket: EntryTicket, filled: int) -> None:
        """The contract is held while its price is pending, so its stop is checked
        every step, as while the entry works (delta-3 review, N3-5):
        - protecting, or filled (the position is already sold): nothing to do;
        - live but not protecting (held back, cancel pending): it can still sell, so
          nothing goes beside it (AEGIS K-2): halt, which alarms Andre;
        - finished without filling: ONE new stop at the attached stop's price (from
          the entry's limit: no fill price is needed, nothing is booked), recorded as
          the ticket's stop before it is sent, so the fill adopts it and only it is
          asked about from then on. If that one is gone too, nothing more is sent:
          halt, protect by hand. Andre gets ONE alarm per position naming both stops
          (D41 (b)): no halt, no flatten. A send refused before the broker (NotSent)
          alarms nothing; its retry does."""
        stop_id = ticket.stop_order_id
        if stop_id is None:
            return
        pid = ticket.position_id
        replacement = _wait_stop_id(pid)
        try:
            held = self._broker.status(stop_id)
        except NeverPlaced:
            if stop_id != replacement:
                raise  # the attached stop went out with the entry: a fault
            # Its send's outcome was unknown, and the broker says it never got it:
            # there is no stop. Nothing more is sent (grind review G7-3).
            self._halt.trip(
                f"{pid}: no resting stop at the broker while the entry's fill price is "
                f"pending (stop {stop_id} never reached the broker): protect the "
                "position by hand"
            )
            return
        if held.protects or held.state is OrderState.FILLED:
            return
        if held.state is OrderState.WORKING:
            self._halt.trip(
                f"{pid}: stop {stop_id} is live but "
                f"{'held back' if held.held_back else 'being cancelled'} while the entry's "
                "fill price is pending: nothing is placed beside it"
            )
            return
        if held.filled > 0:  # it sold part, then ended: nothing beside it (G6-1)
            self._halt.trip(
                f"{pid}: {_partly_sold(stop_id, held)} (the entry's fill price is pending)"
            )
            return
        if stop_id == replacement:
            self._halt.trip(
                f"{pid}: no resting stop at the broker while the entry's fill price is "
                f"pending (stop {stop_id} is {held.state.value}): protect the position "
                "by hand"
            )
            return
        log.critical(
            "attached stop %s is %s while entry %s waits for its price: placing a new one",
            stop_id,
            held.state.value,
            ticket.order_id,
        )
        spec = self._stops.preview(ticket.limit, filled)
        ticket.stop_order_id = replacement  # before the send: never a second beside it
        try:
            placed = self._broker.place(
                Order(
                    order_id=replacement,
                    contract_id=held.order.contract_id,
                    side=Side.SELL,
                    quantity=filled,
                    order_type=OrderType.STOP_LIMIT,
                    limit_price=spec.limit,
                    stop_price=spec.trigger,
                    oca_group=pid,
                )
            )
        except NotSent:
            # Refused before it reached the broker: it does not exist there. The old
            # stop is asked about again, so the next step tries again (G6-2). Any other
            # failure may have reached the broker: its id is kept.
            ticket.stop_order_id = stop_id
            raise
        except Exception as exc:
            self._alarm_wait_stop(
                pid,
                stop_id,
                held.state,
                f"sent, but its outcome is unknown: {exc!r}; it may be live or filled: "
                "check IBKR before placing any stop by hand",
            )
            raise
        self._alarm_wait_stop(pid, stop_id, held.state, _replacement_outcome(placed))

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
        if filled < 1:
            ticket.outcome = EntryOutcome.UNFILLED
            log.info("entry %s ended unfilled (%s)", ticket.order_id, state.value)
            self._end_attached(ticket)
            return ticket
        if price is None:  # filled, its price not reported yet: never "unfilled" (N-1)
            return self._price_pending(ticket, filled, now)
        plan = ticket.approved.plan
        session = session_at(now, self._p.instrument)
        protected = False
        stop: Order | None = None
        kept: Order | None = None  # the stop the position keeps: live or filled at the broker
        stop_id = ticket.stop_order_id or _attached_stop_id(ticket.position_id)
        opened = False
        try:
            self._stops.open(ticket.position_id, price, filled)
            opened = True
            spec = self._stops.resting_order(ticket.position_id)
            if ticket.stop_order_id is not None:
                stop, protected = self._adopt_attached(ticket.stop_order_id, spec, filled)
                kept = stop
            if not protected and stop is None:
                # No attached stop working: a new one (its own id: ids are never reused).
                stop_id = (
                    _fill_stop_id(ticket.position_id)
                    if ticket.stop_order_id
                    else _attached_stop_id(ticket.position_id)
                )
                stop = Order(
                    order_id=stop_id,
                    contract_id=plan.choice.quote.contract_id,
                    side=Side.SELL,
                    quantity=filled,
                    order_type=OrderType.STOP_LIMIT,
                    limit_price=spec.limit,
                    stop_price=spec.trigger,
                    oca_group=ticket.position_id,
                )
                stop_status = self._broker.place(stop)
                protected = stop_status.state in (OrderState.WORKING, OrderState.FILLED)
                kept = stop if protected else None
        except Exception as exc:  # any failure here: find out what the broker really has
            log.critical("resting stop for %s uncertain: %r", ticket.position_id, exc)
            try:
                if not opened:  # no stop level for this fill: it cannot be managed
                    raise RuntimeError("the fill could not open a stop") from exc
                held = self._broker.status(stop_id)  # did it reach the broker after all?
                if held.state in (OrderState.WORKING, OrderState.FILLED):
                    # Adopt it (a fill is handled next). Live but not protecting (held
                    # back, cancel pending): kept, so management exits through its
                    # cancel, never beside it (AEGIS K-2).
                    kept = held.order
                    protected = held.protects or held.state is OrderState.FILLED
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
            # The attached stop when it is live but not protecting: management then
            # sees it and gets out through its cancel (never a sell beside it).
            stop_order_id=kept.order_id if kept is not None else None,
        )
        ticket.outcome = EntryOutcome.PROTECTED if protected else EntryOutcome.UNPROTECTED
        if not protected:
            log.critical("broker refused the resting stop for %s: flatten now", ticket.position_id)
        return ticket
