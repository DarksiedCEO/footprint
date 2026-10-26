"""PR9: the one record of every order sent. Recorded before sending; forgotten only on
the broker's word that it finished; a lost answer resolved by re-sending under the
same id."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    DuplicateOrderId,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Quote,
    Side,
    SimulatedBroker,
    TrackedBroker,
)

D = Decimal
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def setup() -> tuple[SimulatedBroker, TrackedBroker]:
    book = QuoteBook()
    book.publish(Quote("C", D(100), D(102), T))
    sim = SimulatedBroker(book, 5)
    return sim, TrackedBroker(sim)


def sell(order_id: str, price: int = 200) -> Order:
    return Order(order_id, "C", Side.SELL, 1, OrderType.LIMIT, D(price))


def test_an_order_is_followed_until_the_broker_says_it_finished() -> None:
    _, tracked = setup()
    tracked.place(sell("a"))
    assert set(tracked.unfinished) == {"a"}
    assert set(tracked.working()) == {"a"}
    tracked.cancel("a")
    assert tracked.unfinished == {}
    assert tracked.working() == {}


def test_a_send_that_never_arrived_is_placed_by_the_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, tracked = setup()
    real = sim.place
    calls = {"n": 0}

    def hiccup(order: Order) -> OrderStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("dropped before the broker")
        return real(order)

    monkeypatch.setattr(sim, "place", hiccup)
    status = tracked.place(sell("a"))
    assert status.state is OrderState.WORKING
    assert len(sim._orders) == 1


def test_a_lost_answer_is_resolved_by_the_duplicate_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sim, tracked = setup()
    real = sim.place
    calls = {"n": 0}

    def lost(order: Order) -> OrderStatus:
        calls["n"] += 1
        status = real(order)  # arrives (the second time: a duplicate id)
        if calls["n"] == 1:
            raise TimeoutError("answer lost")
        return status

    monkeypatch.setattr(sim, "place", lost)
    status = tracked.place(sell("a"))  # no error: the duplicate proved it arrived
    assert status.state is OrderState.WORKING
    assert status.order.order_id == "a"
    assert len(sim._orders) == 1


def test_when_the_resend_fails_too_the_order_stays_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, tracked = setup()
    real = sim.place
    calls = {"n": 0}

    def gone(order: Order) -> OrderStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            real(order)
        raise TimeoutError("no answer")

    monkeypatch.setattr(sim, "place", gone)
    with pytest.raises(TimeoutError):
        tracked.place(sell("a"))
    assert set(tracked.unfinished) == {"a"}  # its fate is unknown: still followed
    assert set(tracked.working()) == {"a"}


def test_a_caller_reusing_an_id_is_an_error_never_a_resend() -> None:
    sim, tracked = setup()
    tracked.place(sell("a"))
    with pytest.raises(DuplicateOrderId):
        tracked.place(sell("a", 300))
    assert sim.status("a").order.limit_price == D(200)  # the first order is untouched


def test_an_unreadable_status_counts_as_working(monkeypatch: pytest.MonkeyPatch) -> None:
    sim, tracked = setup()
    tracked.place(sell("a"))

    def blind(order_id: str) -> OrderStatus:
        raise ConnectionError("no answer")

    monkeypatch.setattr(sim, "status", blind)
    working = tracked.working()
    assert working["a"].state is OrderState.WORKING
    assert working["a"].filled == 0


def test_a_late_listed_order_is_still_working() -> None:
    sim, tracked = setup()
    tracked.place(sell("a"))
    sim.open_orders = lambda: []  # type: ignore[method-assign]  # the list lags
    assert set(tracked.working()) == {"a"}
