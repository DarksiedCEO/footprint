"""H-2 (AEGIS re-audit), as Andre chose: the hard stop is attached to the entry, so the
broker activates it the moment the entry fills, even while we are disconnected."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.decision.stop import StopOrderSpec
from footprint.execution.executor.diego import EntryOutcome, EntryRefused
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Quote,
    Side,
    SimulatedBroker,
    TrackedBroker,
)
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import SEND, Stack
from tests.execution.test_disconnect_policy import Outage
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)
C = "CALL"


# --- the simulator's bracket -----------------------------------------------------------------


def sim() -> tuple[SimulatedBroker, QuoteBook]:
    book = QuoteBook()
    book.publish(Quote(C, D(150), D(152), SEND))
    return SimulatedBroker(book, 5), book


def bracket(limit: int = 151) -> tuple[Order, Order]:
    entry = Order("p1-entry", C, Side.BUY, 1, OrderType.LIMIT, D(limit))
    stop = Order("p1-stop", C, Side.SELL, 1, OrderType.STOP_LIMIT, D(109), D(114), "p1", "p1-entry")
    return entry, stop


def test_the_attached_stop_is_held_until_the_entry_fills() -> None:
    broker, book = sim()
    entry, stop = bracket()
    first, second = broker.place_attached(entry, stop)
    assert (first.state, second.state) == (OrderState.WORKING, OrderState.WORKING)
    book.publish(Quote(C, D(100), D(160), SEND + SEC))  # bid under the stop: held, no trigger
    assert broker.status("p1-stop").filled == 0
    assert not broker.status("p1-stop").triggered
    book.publish(Quote(C, D(150), D(151), SEND + 2 * SEC))  # the entry fills
    assert broker.status("p1-entry").state is OrderState.FILLED
    assert broker.status("p1-stop").state is OrderState.WORKING  # now active
    book.publish(Quote(C, D(113), D(115), SEND + 3 * SEC))  # the stop triggers and fills
    assert broker.status("p1-stop").state is OrderState.FILLED
    assert broker.positions() == {}


def test_an_unfilled_entry_takes_its_attached_stop_with_it() -> None:
    broker, _ = sim()
    entry, stop = bracket()
    broker.place_attached(entry, stop)
    broker.cancel("p1-entry")
    assert broker.status("p1-stop").state is OrderState.CANCELLED
    assert broker.open_orders() == []


def test_an_entry_that_fills_at_once_activates_its_stop_at_once() -> None:
    broker, _ = sim()
    entry, stop = bracket(limit=152)  # the ask is 152: fills on arrival
    first, _ = broker.place_attached(entry, stop)
    assert first.state is OrderState.FILLED
    assert broker.status("p1-stop").state is OrderState.WORKING
    assert broker.positions() == {C: 1}


def test_a_child_must_name_its_parent() -> None:
    broker, _ = sim()
    entry, _ = bracket()
    loose = Order("p1-stop", C, Side.SELL, 1, OrderType.STOP_LIMIT, D(109), D(114), "p1")
    with pytest.raises(ValueError, match="not attached"):
        broker.place_attached(entry, loose)
    with pytest.raises(ValueError, match="not attached"):
        TrackedBroker(broker).place_attached(entry, loose)
    with pytest.raises(ValueError, match="another order"):
        Order("x", C, Side.SELL, 1, OrderType.LIMIT, D(1), parent_id="x")


# --- Diego --------------------------------------------------------------------------------------


def test_the_entry_goes_out_with_its_stop_attached(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop = stack.broker.status(f"{ticket.position_id}-stop")
    assert stop.order.parent_id == ticket.order_id
    assert stop.order.oca_group == ticket.position_id
    spec = stack.stops.preview(stack.approved.plan.limit_floor, stack.approved.plan.contracts)
    assert (stop.order.stop_price, stop.order.limit_price) == (spec.trigger, spec.limit)
    assert ticket.stop_order_id == stop.order.order_id


def test_each_price_step_raises_the_stop_before_the_entry(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.broker.log.clear()
    stack.executor.on_time(ticket, SEND + 10 * SEC)  # steps to the ceiling
    modified = [line for line in stack.broker.log if line.startswith("modified")]
    assert modified[0].startswith(f"modified {ticket.position_id}-stop")
    assert modified[1].startswith(f"modified {ticket.order_id}")
    ceiling = stack.approved.plan.limit_ceiling
    spec = stack.stops.preview(ceiling, 1)
    stop = stack.broker.status(f"{ticket.position_id}-stop").order
    assert stop.stop_price == spec.trigger  # follows the highest price it can fill at


def test_on_the_fill_the_attached_stop_is_kept_and_moved_to_the_real_fill(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)  # filled at the 151 floor
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert ticket.position is not None
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop"
    spec = stack.stops.resting_order(ticket.position_id)
    stop = stack.broker.status(ticket.position.stop_order_id).order
    assert (stop.stop_price, stop.limit_price) == (spec.trigger, spec.limit)
    stops = [o for o in stack.broker.open_orders() if o.order_type is OrderType.STOP_LIMIT]
    assert len(stops) == 1  # one stop, never a second beside it


def test_an_unfilled_entry_leaves_no_stop_behind(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    timeout = stack.profiles.execution.entry.timeout_seconds
    ticket = stack.executor.on_time(ticket, SEND + timeout * SEC)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.open_orders() == []
    assert stack.executor._broker.unfinished == {}


def test_a_dead_attached_stop_is_replaced_on_the_fill(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.broker.cancel(f"{ticket.position_id}-stop")  # e.g. cancelled at the broker by hand
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert ticket.position is not None
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop-2"


def test_no_entry_without_a_stop_that_fits(stack: Stack) -> None:
    def no_fit(fill: Decimal, contracts: int) -> StopOrderSpec:
        raise ValueError("too small to carry the hard stop")

    stack.stops.preview = no_fit  # type: ignore[method-assign]
    with pytest.raises(EntryRefused, match="no hard stop can be attached"):
        stack.executor.submit(stack.approved, (), SEND)
    assert stack.broker.open_orders() == []


# --- the disconnect case itself -------------------------------------------------------------


def test_an_entry_that_fills_during_an_outage_is_protected_at_the_broker(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The entry is working when IBKR is lost; the market fills it, then falls through
    the stop, all during the outage: the broker's attached stop takes it out."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    t = DECISION_TIME + SEC
    runner.step(t)  # the entry is sent (working)
    assert any(o.order_id.endswith("-entry") for o in stack.broker.open_orders())
    outage = Outage(stack.broker)
    outage.start()
    held = outage._saved["positions"]  # what the exchange really holds, past the outage
    stack.quote(150, D(151), t + SEC)  # filled at the exchange during the outage
    assert held() != {}
    assert [o.order_type for o in outage.resting()] == [OrderType.STOP_LIMIT]  # protected
    stack.quote(113, 115, t + 2 * SEC)  # the market falls through the stop (113.5)
    assert held() == {}  # the broker's stop sold it, with us away
    assert outage.attempts == []
    for k in range(3, 30):
        runner.step(t + k * SEC)  # still away
    outage.end()
    for k in range(30, 50):
        stack.quote(113, 115, t + k * SEC)
        runner.step(t + k * SEC)
    assert stack.broker.positions() == {}
    assert not runner.reconciling


def test_a_fill_during_an_outage_is_adopted_with_its_stop_after_the_reconnect(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The entry fills during the outage and the stop is not reached: protected at the
    broker throughout. After the reconnect the runner settles the entry before it
    reconciles: the fill is the position (booked at its real price, its attached stop
    kept), never forgotten and flattened as unrecorded (delta-3 review, N3-1; before
    that, this test pinned the flatten)."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    t = DECISION_TIME + SEC
    runner.step(t)  # the entry is sent (working)
    outage = Outage(stack.broker)
    outage.start()
    held = outage._saved["positions"]
    stack.quote(150, D(151), t + SEC)  # filled at the exchange during the outage
    for k in range(2, 20):
        stack.quote(150, D(151), t + k * SEC)
        runner.step(t + k * SEC)
        assert [o.order_type for o in outage.resting()] == [OrderType.STOP_LIMIT]
    assert held() != {}
    outage.end()
    for k in range(20, 60):
        stack.quote(150, D(151), t + k * SEC)
        runner.step(t + k * SEC)
    position = runner.position
    assert position is not None
    assert position.entry_price == D(151)
    assert stack.ledger.open_position_id == position.position_id
    assert stack.broker.positions() == {stack.contract: 1}  # kept, not sold
    assert [o.order_id for o in stack.broker.open_orders()] == [position.stop_order_id]
    assert not runner.reconciling  # reconcile agrees with the adopted position
    assert not any("never recorded" in a.message for a in alarms)


def test_a_fill_below_the_limit_moves_the_stop_down_to_the_real_fill(stack: Stack) -> None:
    """Priced from the limit (151.5) while working; filled at 151.25 (the ask when the
    step arrives): the stop moves to the 151.25 fill's level."""
    ticket = stack.executor.submit(stack.approved, (), SEND)  # floor 151, ask 152: rests
    stack.quote(150, D("151.25"), SEND + SEC)  # ask still above the 151 limit
    ticket = stack.executor.on_time(ticket, SEND + 10 * SEC)  # steps to 151.5: fills at 151.25
    assert ticket.position is not None
    assert ticket.position.entry_price == D("151.25")
    at_fill = stack.stops.resting_order(ticket.position_id)
    at_limit = stack.stops.preview(D("151.5"), 1)
    assert at_fill.trigger < at_limit.trigger
    stop = stack.broker.status(f"{ticket.position_id}-stop").order
    assert (stop.stop_price, stop.limit_price) == (at_fill.trigger, at_fill.limit)


def test_preview_refuses_a_price_too_small_to_carry_the_stop(stack: Stack) -> None:
    tick = stack.profiles.instrument.option_tick_size
    with pytest.raises(ValueError, match="too small"):
        stack.stops.preview(tick, 1)
    with pytest.raises(ValueError, match="max_contracts"):
        stack.stops.preview(D(151), stack.profiles.risk.max_contracts + 1)
    with pytest.raises(ValueError, match="tick"):
        stack.stops.preview(D("151.1"), 1)


# --- AEGIS final audit: HIGH-1 and the surviving bracket mutants -----------------------------


def test_an_entry_whose_attached_stop_is_refused_is_cancelled_at_once(stack: Stack) -> None:
    """HIGH-1: the entry must never work without its stop at the broker."""
    original = stack.broker.place_attached

    def stop_refused(parent: Order, child: Order) -> tuple[OrderStatus, OrderStatus]:
        first, _ = original(parent, child)
        stack.broker.cancel(child.order_id)  # refused at the broker
        return first, stack.broker.status(child.order_id)

    stack.broker.place_attached = stop_refused  # type: ignore[method-assign]
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(ticket.order_id).state is OrderState.CANCELLED
    assert stack.broker.open_orders() == []


def test_an_entry_whose_attached_stop_dies_while_it_works_is_cancelled(stack: Stack) -> None:
    """HIGH-1: checked every step: a stop cancelled at the broker (by IBKR, or by hand)
    takes the entry down before the entry can fill unprotected."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.broker.cancel(f"{ticket.position_id}-stop")
    ticket = stack.executor.on_time(ticket, SEND + SEC)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(ticket.order_id).state is OrderState.CANCELLED
    stack.quote(150, D(151), SEND + 2 * SEC)  # the market comes to it: nothing fills
    assert stack.broker.positions() == {}


def test_a_stop_that_outlives_its_unfilled_entry_is_cancelled(stack: Stack) -> None:
    """A4: a broker that did not take the stop down with the entry."""
    stack.broker._release_children = lambda parent_id, parent: None  # type: ignore[method-assign]
    ticket = stack.executor.submit(stack.approved, (), SEND)
    timeout = stack.profiles.execution.entry.timeout_seconds
    ticket = stack.executor.on_time(ticket, SEND + timeout * SEC)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(f"{ticket.position_id}-stop").state is OrderState.CANCELLED


def test_an_attached_stop_for_another_size_is_cancelled_not_kept_beside(stack: Stack) -> None:
    """A5: a size mismatch means a new stop: the old one must not stay beside it."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    spec = stack.stops.preview(D(151), 1)
    order, protected = stack.executor._adopt_attached(stop_id, spec, filled=2)
    assert (order, protected) == (None, False)
    assert stack.broker.status(stop_id).state is OrderState.CANCELLED


def test_an_attached_stop_the_move_kills_is_not_counted_as_protection(stack: Stack) -> None:
    """A7: if moving the stop to the real fill leaves it not working, a new stop."""
    original = stack.broker.modify

    def kills_stops(order_id: str, limit_price: Decimal, stop_price: Decimal | None) -> OrderStatus:
        if order_id.endswith("-stop") and stop_price == D("113.75"):  # the move to the fill
            return stack.broker.cancel(order_id)  # ...ends it
        return original(order_id, limit_price, stop_price)

    stack.broker.modify = kills_stops  # type: ignore[method-assign]
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D("151.25"), SEND + SEC)
    ticket = stack.executor.on_time(ticket, SEND + 10 * SEC)  # fills at 151.25 < limit
    assert ticket.position is not None
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop-2"
    assert ticket.outcome is EntryOutcome.PROTECTED


def test_an_attached_stop_that_already_filled_is_not_doubled(stack: Stack) -> None:
    """A8: entry and stop both filled before the fill is seen: no second stop."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND + SEC)  # the entry fills at 151
    stack.quote(113, 115, SEND + 2 * SEC)  # and the attached stop sells at 113
    assert stack.broker.positions() == {}
    ticket = stack.executor.on_time(ticket, SEND + 3 * SEC)
    assert ticket.position is not None
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop"
    with pytest.raises(Exception, match="stop-2"):
        stack.broker.status(f"{ticket.position_id}-stop-2")  # never placed
    assert stack.broker.positions() == {}


def test_a_bracket_that_never_left_is_not_kept_in_the_record() -> None:
    """A9: NotSent means neither order reached the broker: the record drops both."""
    from footprint.execution.orders import NotSent

    class Refuses(SimulatedBroker):
        def place_attached(self, parent: Order, child: Order) -> tuple[OrderStatus, OrderStatus]:
            raise NotSent("not connected")

    broker, book = sim()
    tracked = TrackedBroker(Refuses(book, 5))
    entry, stop = bracket()
    with pytest.raises(NotSent):
        tracked.place_attached(entry, stop)
    assert tracked.unfinished == {}
    assert broker is not None


def test_a_cancelled_held_stop_never_comes_back_when_the_entry_fills() -> None:
    """A10: a held stop cancelled on its own stays cancelled: no activation, no fill."""
    broker, book = sim()
    entry, stop = bracket()
    broker.place_attached(entry, stop)
    broker.cancel("p1-stop")
    book.publish(Quote(C, D(150), D(151), SEND + SEC))  # the entry fills
    book.publish(Quote(C, D(113), D(115), SEND + 2 * SEC))
    assert broker.status("p1-stop").state is OrderState.CANCELLED
    assert broker.status("p1-stop").filled == 0
    assert not any(line.startswith("activated p1-stop") for line in broker.log)
    assert broker.positions() == {C: 1}


# --- AEGIS 4c11b67 audit: N1/N2 (held back), G3, G16 -------------------------------------------


def _hold_back(stack: Stack, stop_id: str) -> None:
    """The attached stop is reported held back (Inactive, cancel unanswered)."""
    original = stack.broker.status

    def held(order_id: str) -> OrderStatus:
        status = original(order_id)
        if order_id == stop_id and status.state is OrderState.WORKING:
            from dataclasses import replace as swap

            return swap(status, held_back=True)
        return status

    stack.broker.status = held  # type: ignore[method-assign]


def test_a_held_back_attached_stop_cancels_the_working_entry(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    _hold_back(stack, f"{ticket.position_id}-stop")
    ticket = stack.executor.on_time(ticket, SEND + SEC)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(ticket.order_id).state is OrderState.CANCELLED


def test_a_held_back_attached_stop_is_kept_never_doubled_on_the_fill(stack: Stack) -> None:
    """AEGIS K-2: a held-back attached stop is still live at the broker (it can come
    back and sell): no second stop goes beside it. The position keeps it, unprotected,
    and management gets out through its cancel."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)  # filled; the stop activates...
    _hold_back(stack, f"{ticket.position_id}-stop")  # ...but is reported held back
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop"
    assert f"{ticket.position_id}-stop-2" not in stack.broker._orders  # never placed


def test_malik_flattens_a_position_whose_stop_is_held_back(stack: Stack) -> None:
    from footprint.execution.management.malik import ExitReason

    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.position is not None
    _hold_back(stack, ticket.position.stop_order_id or "")
    closed = stack.step(ticket.position, stack.quote(150, D(151), SEND + SEC), (), SEND + SEC)
    assert closed is not None
    assert closed.reason is ExitReason.UNPROTECTED
    assert any("held back" in a.message for a in stack.alarms)


def test_a_fill_racing_the_entry_cancel_still_gets_a_stop(stack: Stack) -> None:
    """G3: the stop dies, the entry is cancelled, but it filled first: the fill path
    places a new stop (or flattens), never 'unfilled'."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    original_cancel = stack.broker.cancel

    def fills_first(order_id: str) -> OrderStatus:
        if order_id == ticket.order_id:
            stack.quote(150, D(151), SEND + SEC)  # the entry fills just before the cancel
        return original_cancel(order_id)

    stack.broker.cancel(stop_id)  # the attached stop dies
    stack.broker.cancel = fills_first  # type: ignore[method-assign]
    ticket = stack.executor.on_time(ticket, SEND + SEC)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop-2"


def test_a_cancelled_attached_stop_is_never_adopted(stack: Stack) -> None:
    """G16: adoption judges the broker's state, not just "not filled"."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    stack.broker.cancel(stop_id)
    spec = stack.stops.preview(D(151), 1)
    assert stack.executor._adopt_attached(stop_id, spec, filled=1) == (None, False)


# --- AEGIS 3f3ee04 audit: F1 (H9), H6 --------------------------------------------------------


def test_a_held_back_stop_whose_cancel_never_lands_escalates(stack: Stack) -> None:
    """F1: the position is held, its stop held back, and the stop's cancel never lands:
    management must not wait forever: after cancel_escalate_seconds it hands over to
    reconcile (which escalates to Andre), with one alarm, not one a second."""
    from footprint.execution.management.malik import NeedsReconcile

    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.position is not None
    stop_id = ticket.position.stop_order_id or ""
    _hold_back(stack, stop_id)
    stack.broker.cancel = lambda order_id: stack.broker.status(order_id)  # type: ignore[method-assign]
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    raised_at = None
    for k in range(1, escalate + 5):
        t = SEND + k * SEC
        try:
            stack.step(ticket.position, stack.quote(150, D(151), t), (), t)
        except NeedsReconcile:
            raised_at = k
            break
    assert raised_at is not None, "the exit waited forever"
    assert raised_at <= escalate + 2
    held_back = [a for a in stack.alarms if "held back" in a.message]
    not_cancelled = [a for a in stack.alarms if "resting stop not cancelled" in a.message]
    assert len(held_back) == 1
    assert len(not_cancelled) == 1  # through the stop's own stuck-stop escalation
    assert stack.broker.positions() != {}  # nothing was sold beside the stuck stop


def test_a_stop_held_back_on_arrival_means_no_entry(stack: Stack) -> None:
    """H6: held back already when the bracket is answered: the entry is cancelled at once."""
    original = stack.broker.place_attached

    def held_on_arrival(parent: Order, child: Order) -> tuple[OrderStatus, OrderStatus]:
        from dataclasses import replace as swap

        first, second = original(parent, child)
        return first, swap(second, held_back=True)

    stack.broker.place_attached = held_on_arrival  # type: ignore[method-assign]
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(ticket.order_id).state is OrderState.CANCELLED


def test_an_exit_never_waits_forever_on_another_order(stack: Stack) -> None:
    """F1, the generic case: the stop is gone, but another sell on the contract will
    not cancel: the exit waits at most cancel_escalate_seconds, then reconcile."""
    from footprint.execution.management.malik import NeedsReconcile

    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.position is not None
    stack.broker.cancel(ticket.position.stop_order_id or "")  # the stop is gone
    other = Order("hand-sell", ticket.position.contract_id, Side.SELL, 1, OrderType.LIMIT, D(400))
    stack.executor._broker.place(other)  # a sell nobody can cancel
    original = stack.broker.cancel
    stack.broker.cancel = lambda order_id: (  # type: ignore[method-assign]
        stack.broker.status(order_id) if order_id == "hand-sell" else original(order_id)
    )
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    raised_at = None
    for k in range(1, escalate + 5):
        t = SEND + k * SEC
        try:
            stack.step(ticket.position, stack.quote(150, D(151), t), (), t)
        except NeedsReconcile:
            raised_at = k
            break
    assert raised_at is not None, "the exit waited forever"
    assert len([a for a in stack.alarms if "exit waits" in a.message]) == 1
    assert stack.halt.tripped  # (AEGIS I-N7: the escalation stands entries down)


def _mark(stack: Stack, stop_id: str, *, held_back: bool = False, cancelling: bool = False) -> None:
    """The stop is reported live but held back, or with its cancel pending."""
    from dataclasses import replace as swap

    original = stack.broker.status

    def marked(order_id: str) -> OrderStatus:
        status = original(order_id)
        if order_id == stop_id and status.state is OrderState.WORKING:
            return swap(status, held_back=held_back, cancelling=cancelling)
        return status

    stack.broker.status = marked  # type: ignore[method-assign]


def test_an_attached_stop_being_cancelled_is_kept_never_doubled_on_the_fill(
    stack: Stack,
) -> None:
    """AEGIS K-2: an attached stop whose cancel is pending can still fill: a second stop
    beside it could sell twice. The position keeps it and management exits around it."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stack.quote(150, D(151), SEND)
    _mark(stack, f"{ticket.position_id}-stop", cancelling=True)
    ticket = stack.executor.on_time(ticket, SEND)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop"
    assert f"{ticket.position_id}-stop-2" not in stack.broker._orders


def test_a_move_that_leaves_the_attached_stop_held_back_never_doubles_it(stack: Stack) -> None:
    """The move to the fill leaves the stop live but held back: kept, not doubled."""
    original = stack.broker.modify
    held: set[str] = set()

    def holds_back(order_id: str, limit_price: Decimal, stop_price: Decimal | None) -> OrderStatus:
        from dataclasses import replace as swap

        status = original(order_id, limit_price, stop_price)
        if order_id.endswith("-stop") and stop_price == D("113.75"):
            held.add(order_id)
            return swap(status, held_back=True)
        return status

    stack.broker.modify = holds_back  # type: ignore[method-assign]
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    from dataclasses import replace as swap

    plain = stack.broker.status

    def reported(order_id: str) -> OrderStatus:
        status = plain(order_id)
        return swap(status, held_back=True) if order_id in held else status

    stack.broker.status = reported  # type: ignore[method-assign]
    stack.quote(150, D("151.25"), SEND + SEC)
    ticket = stack.executor.on_time(ticket, SEND + 10 * SEC)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position.stop_order_id == stop_id
    assert f"{stop_id}-2" not in stack.broker._orders


def test_the_uncertain_stop_path_never_counts_a_held_back_stop_as_protection(
    stack: Stack,
) -> None:
    """AEGIS K: the move to the fill raises; the broker then reports the attached stop
    live but held back: kept (nothing beside it), but UNPROTECTED, never PROTECTED."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    original = stack.broker.modify

    def fails(order_id: str, limit_price: Decimal, stop_price: Decimal | None) -> OrderStatus:
        if order_id == stop_id and stop_price == D("113.75"):
            _mark(stack, stop_id, held_back=True)
            raise ConnectionError("lost mid-move")
        return original(order_id, limit_price, stop_price)

    stack.broker.modify = fails  # type: ignore[method-assign]
    stack.quote(150, D("151.25"), SEND + SEC)
    ticket = stack.executor.on_time(ticket, SEND + 10 * SEC)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position.stop_order_id == stop_id
    assert f"{stop_id}-2" not in stack.broker._orders


def test_an_attached_stop_for_another_size_that_will_not_cancel_is_kept_not_doubled(
    stack: Stack,
) -> None:
    """AEGIS L2-6: the wrong-size stop's cancel is not confirmed: it can still sell, so
    no new stop goes beside it."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    stack.broker.cancel = stack.broker.status  # type: ignore[method-assign]  # never lands
    spec = stack.stops.preview(D(151), 1)
    order, protected = stack.executor._adopt_attached(stop_id, spec, filled=2)
    assert order is not None
    assert order.order_id == stop_id
    assert not protected
