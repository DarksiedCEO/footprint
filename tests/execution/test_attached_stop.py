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


def test_a_fill_during_an_outage_is_flattened_after_the_reconnect(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The entry fills during the outage and the stop is not reached: protected at the
    broker throughout; after the reconnect the reconcile flattens it, stop and all."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
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
    assert stack.broker.positions() == {}  # flattened by the reconcile
    assert stack.broker.open_orders() == []  # the stop went with it
    assert not runner.reconciling


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
