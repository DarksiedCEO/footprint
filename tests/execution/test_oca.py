"""One-cancels-all groups: a position's stop and exits can never both fill."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from footprint.execution.market import QuoteBook
from footprint.execution.orders import Order, OrderState, OrderType, Quote, Side, SimulatedBroker
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position

D = Decimal
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def broker() -> tuple[QuoteBook, SimulatedBroker]:
    book = QuoteBook()
    book.publish(Quote("C", D(100), D(102), T))
    return book, SimulatedBroker(book, 5)


def test_when_one_fills_the_rest_of_its_group_is_cancelled() -> None:
    book, sim = broker()
    sim.place(Order("a", "C", Side.SELL, 1, OrderType.LIMIT, D(105), oca_group="g"))
    sim.place(Order("b", "C", Side.SELL, 1, OrderType.LIMIT, D(110), oca_group="g"))
    sim.place(Order("c", "C", Side.SELL, 1, OrderType.LIMIT, D(110)))  # not in the group
    book.publish(Quote("C", D(111), D(112), T.replace(second=1)))  # both would cross
    states = {i: sim.status(i).state for i in "abc"}
    assert sorted(s.value for s in (states["a"], states["b"])) == ["CANCELLED", "FILLED"]
    assert states["c"] is OrderState.FILLED


def test_an_order_placed_into_a_filled_group_is_cancelled_at_once() -> None:
    _, sim = broker()
    sim.place(Order("a", "C", Side.SELL, 1, OrderType.LIMIT, D(100), oca_group="g"))
    assert sim.status("a").state is OrderState.FILLED
    late = sim.place(Order("b", "C", Side.SELL, 1, OrderType.LIMIT, D(100), oca_group="g"))
    assert late.state is OrderState.CANCELLED
    assert sim.positions() == {"C": -1}  # only one of them ever sold


def test_a_positions_stop_and_exits_share_its_group(stack: Stack) -> None:
    position = filled_position(stack)
    stop = stack.broker.status(f"{position.position_id}-stop")
    assert stop.order.oca_group == position.position_id
    stack.ledger.latch_kill_switch("test")
    t = FILLED_AT + timedelta(seconds=60)
    stack.quote(150, 152, t)
    stack.manager.manage(position, (), t)
    exits = [s for s in stack.broker._orders.values() if "-exit-" in s.order.order_id]
    assert exits
    assert all(s.order.oca_group == position.position_id for s in exits)


def test_position_ids_are_unique_even_in_the_same_second(stack: Stack) -> None:
    from footprint.execution.executor.diego import Executor

    other = Executor(
        stack.profiles,
        stack.pipeline,
        stack.manager._broker,
        stack.stops,
        stack.ledger,
        stack.halt,
        stack.book,
    )
    assert other._sequence == stack.executor._sequence == 0  # both would number from 1
    first = filled_position(stack)
    assert first.position_id.count("-") == 2  # time-sequence-random
    assert len(first.position_id.rsplit("-", 1)[1]) == 8
