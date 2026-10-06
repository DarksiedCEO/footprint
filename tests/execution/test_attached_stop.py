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
