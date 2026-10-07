"""Independent delta review of f44b0e4 (claude/audit-2026-10-07-delta.md): F-1, F-3,
F-4 and F-5. A fill's price comes from its executions when they cover it (or IBKR's own
real average for exactly that size), and is never guessed from part of it or from 0. A
cancel lost as IBKR drops is sent again once IBKR is ready."""

from decimal import Decimal

import pytest

from footprint.execution.orders import Order, OrderState, OrderType, Side
from tests.brokers.test_ibkr_adapter import C, Ib, make

D = Decimal


@pytest.fixture
def ib() -> Ib:
    return make()


def _resting(ib: Ib, order_id: str = "p1-entry", quantity: int = 1) -> tuple[int, int]:
    ib.adapter.place(Order(order_id, C, Side.BUY, quantity, OrderType.LIMIT, D(140)))
    copy = next(c for c in ib.fake.copies.values() if c.request.order_ref == order_id)
    return copy.ib_id, copy.perm_id


def _execution(ib: Ib, n: int, ib_id: int, perm: int, shares: int, price: str) -> None:
    ib.adapter.on_execution(
        f"0000f{n:04x}.65a1b2c3.01.01", ib_id, perm, 501, "NQ", "FOP", "BOT", shares, D(price)
    )


def test_a_fill_is_priced_from_every_execution_never_part_of_them(ib: Ib) -> None:
    """F-1: Filled 2 at price 0 after one execution, then the second execution."""
    ib_id, perm = _resting(ib, quantity=2)
    _execution(ib, 1, ib_id, perm, 1, "140")
    ib.adapter.on_order_status(ib_id, perm, "Filled", 2, 0, D(0))
    assert ib.adapter.status("p1-entry").avg_price is None  # only half of it is known
    _execution(ib, 2, ib_id, perm, 1, "146")
    status = ib.adapter.status("p1-entry")
    assert (status.state, status.filled) == (OrderState.FILLED, 2)
    assert status.avg_price == D(143)


def test_an_execution_arriving_after_a_zero_priced_fill_prices_it(ib: Ib) -> None:
    """F-4: Filled 1 at 0 with no execution yet; the execution then arrives."""
    ib_id, perm = _resting(ib)
    ib.adapter.on_order_status(ib_id, perm, "Filled", 1, 0, D(0))
    assert ib.adapter.status("p1-entry").avg_price is None
    _execution(ib, 3, ib_id, perm, 1, "141")
    assert ib.adapter.status("p1-entry").avg_price == D(141)


def test_an_execution_reported_at_price_zero_is_never_a_price(ib: Ib) -> None:
    """F-5: an execution at 0 is no price, for the copy and for the order."""
    ib_id, perm = _resting(ib)
    _execution(ib, 4, ib_id, perm, 1, "0")
    ib.adapter.on_order_status(ib_id, perm, "Filled", 1, 0, D(0))
    assert ib.adapter.status("p1-entry").avg_price is None


def test_ibkrs_own_average_is_used_only_for_the_size_it_describes(ib: Ib) -> None:
    ib_id, perm = _resting(ib, quantity=2)
    ib.adapter.on_order_status(ib_id, perm, "Filled", 2, 0, D("143.5"))
    assert ib.adapter.status("p1-entry").avg_price == D("143.5")  # real, for both lots


def test_a_cancel_lost_as_ibkr_drops_is_sent_again_once_ready(ib: Ib) -> None:
    """F-3: the Inactive order's cancel raises at the socket; it is not forgotten."""
    ib.adapter.place(Order("p1-stop", C, Side.SELL, 1, OrderType.LIMIT, D(300), oca_group="p1"))
    stop = next(c for c in ib.fake.copies.values() if c.request.order_ref == "p1-stop")
    real_cancel = ib.fake.cancel_order
    calls: list[int] = []

    def dropped(ib_id: int) -> None:
        calls.append(ib_id)
        raise ConnectionError("socket closed")

    ib.fake.cancel_order = dropped  # type: ignore[method-assign]
    ib.resyncs.clear()
    ib.fake.inactive.add(stop.perm_id)
    ib.adapter.on_order_status(stop.ib_id, stop.perm_id, "Inactive", 0, 1, D(0))
    assert calls == [stop.ib_id]  # tried once, and the socket refused it
    assert ib.resyncs  # what IBKR holds is in doubt: reconcile
    ib.fake.cancel_order = real_cancel  # type: ignore[method-assign]
    ib.fake.lose()
    ib.fake.restore(data_lost=True)  # the resync replays it Inactive
    assert ib.adapter.ready
    assert ib.fake.cancels.count(stop.ib_id) == 1  # sent again, once
