"""Grind review of cc98ec0 (PR #21, claude/grind-review-cc98ec0.md), G7-1 (LOW, latent
at `max_contracts: 1`; must close before Andre scales to 2-3 contracts).

A stop that sold part of the position and then ended (cancelled by hand or by IBKR) is
handed by management to reconcile (G6-1). Reconcile then did two wrong things:

- P1: it recognised a sale with its price pending only when the order's state was
  FILLED. A CANCELLED stop that sold 1 of 2, with its price not reported yet and the
  position report lagging at 2, was not a sale: reconcile sold 2 more, a short of 1.
  The same held for a reconcile order of its own (`_unpriced_reconcile_order`).
- P4: once flat, it booked the WHOLE position at the price of the first FILLED sell it
  found (its own reconcile sell), never looking at the stop's partial sale: one
  contract booked at a price it never got (a guessed price, and the loss understated).

Now any sell that has filled, in full or in part, whatever its final state, with no
price yet, holds the contract (D40), and a position sold in parts (or sold twice) is
never booked at one price: Andre books it with `Ledger.book_by_hand`, told what each
sell sold.

These run at the manager level, as the reviewer's probes did: config refuses
`max_contracts` above 1 (`core/config.py`), so no runner can open a 2-contract position
today. The position is made 2 contracts by hand and the broker's answers are reported
as IBKR would (the `Lag` double and a patched stop status)."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import NeedsReconcile
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag, _wait

D = Decimal
SEC = timedelta(seconds=1)
TWO = 2


def _two_contracts(stack: Stack) -> Position:
    position = filled_position(stack)
    position.contracts = TWO
    return position


def _stop_sold_one_of_two(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, stop_id: str, price: Decimal | None
) -> None:
    """The broker reports the stop CANCELLED after it sold 1 of 2 (`price`: the price of
    that sale, or None while IBKR has not reported it yet)."""
    SimulatedBroker.cancel(stack.broker, stop_id)
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked == stop_id:
            order = replace(status.order, quantity=TWO)
            return replace(
                status, order=order, state=OrderState.CANCELLED, filled=1, avg_price=price
            )
        return status

    monkeypatch.setattr(stack.broker, "status", reported)


def _hand_over(stack: Stack, position: Position) -> datetime:
    t = SEND + 60 * SEC
    stack.quote(150, 152, t)
    with pytest.raises(NeedsReconcile, match="sold 1 of 2"):
        stack.manager.manage(position, (), t)
    return t


def _rounds(stack: Stack, position: Position | None, start: datetime, seconds: int) -> datetime:
    """Reconcile every second for `seconds` (several settle waits, past the price wait)."""
    t = start
    for k in range(seconds):
        t = start + k * SEC
        stack.quote(150, 151, t)
        stack.manager.reconcile(position, t)
    return t


def _long_enough(stack: Stack) -> int:
    reconcile = stack.profiles.execution.reconcile
    return int(_wait(stack).total_seconds()) + 8 * (reconcile.settle_seconds + 1)


def _messages(stack: Stack) -> list[str]:
    return [a.message for a in stack.alarms]


# --- P1: a partial sale with its price pending is a sale ---------------------------------


def test_a_stop_that_sold_part_with_its_price_pending_is_never_sold_beside(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe P1: 2 held, the stop sold 1 and was cancelled, its price is
    not reported yet, and the report still says 2. Before: reconcile sold 2 (a short of
    1). Now: nothing is sold; the hold is saved; past the wait, one CHECK THE FILL."""
    lag = Lag(stack.broker, monkeypatch)
    position = _two_contracts(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag.hold({position.contract_id: TWO})  # the report lags the stop's partial sale
    _stop_sold_one_of_two(stack, monkeypatch, stop_id, None)
    t = _hand_over(stack, position)
    _rounds(stack, position, t, _long_enough(stack))
    assert lag.sells() == []  # never a sell beside the stop's sale
    assert [h.order_id for h in stack.ledger.sold_unpriced] == [stop_id]  # saved
    check = [m for m in _messages(stack) if "CHECK THE FILL" in m]
    assert len(check) == 1
    assert stop_id in check[0]
    assert stack.ledger.account(t).kill_switch_latched
    assert stack.ledger.open_position_id == position.position_id  # nothing booked


def test_management_never_sells_beside_a_partial_sale_with_its_price_pending(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit path (`_place_exit`) uses the same test: a sell of the position that
    sold part, price pending, is the position's sale. No exit is sent beside it."""
    lag = Lag(stack.broker, monkeypatch)
    position = _two_contracts(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    order = Order(
        f"{position.position_id}-exit-1",
        position.contract_id,
        Side.SELL,
        TWO,
        OrderType.LIMIT,
        D(150),
    )
    stack.broker._orders[order.order_id] = OrderStatus(order, OrderState.CANCELLED, 1, None)
    stack.manager._exit_attempts[position.position_id] = 1
    assert stack.manager._unpriced_sell(position) == order.order_id
    assert lag.sells() == []


# --- P4: a position sold in parts is never booked at one price ---------------------------


def test_a_position_sold_in_parts_is_never_booked_at_one_price(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe P4: the stop sold 1 of 2 at 140 and was cancelled; the
    report is right at 1; reconcile sells the 1 at 150. Before: the whole position was
    booked at 150 (one contract at a price it never got). Now: nothing is booked, and
    Andre is told each sale, to book it by hand."""
    position = _two_contracts(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    _stop_sold_one_of_two(stack, monkeypatch, stop_id, D(140))
    t = _hand_over(stack, position)
    _rounds(stack, position, t, _long_enough(stack))
    sent = [o for o in stack.broker._orders if o.startswith("reconcile-")]
    assert len(sent) == 1  # the 1 really held, sold once
    assert stack.broker._orders[sent[0]].order.quantity == 1
    assert SimulatedBroker.positions(stack.broker) == {}
    assert stack.manager.take_reconcile_closes() == []  # never booked at one price
    assert stack.ledger.open_position_id == position.position_id
    booked = [m for m in _messages(stack) if "sold in parts" in m]
    assert len(booked) == 1
    assert f"{stop_id} sold 1 of 2" in booked[0]
    assert "at 140" in booked[0]
    assert sent[0] in booked[0]
    assert "Ledger.book_by_hand" in booked[0]


def test_a_position_sold_twice_is_never_booked_at_one_price(stack: Stack) -> None:
    """Two sells of a 1-contract position both FILLED (a sale twice): neither price is
    the position's exit; before, the first one found was booked."""
    position = filled_position(stack)
    pid, contract = position.position_id, position.contract_id
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(
        stop, state=OrderState.FILLED, filled=1, avg_price=D(140)
    )
    exit_order = Order(f"{pid}-exit-1", contract, Side.SELL, 1, OrderType.LIMIT, D(150))
    stack.broker._orders[exit_order.order_id] = OrderStatus(
        exit_order, OrderState.FILLED, 1, D(150)
    )
    stack.manager._exit_attempts[pid] = 1
    _, found = stack.manager._actual_exit(position)
    assert not found
    parts = stack.manager._sold_in_parts(position)
    assert parts is not None
    assert stop_id in parts
    assert exit_order.order_id in parts


def test_one_filled_sell_of_part_of_the_position_does_not_book_it(stack: Stack) -> None:
    """A single sell FILLED at a price but for 1 of the 2 held is not the position's
    exit: the other contract was sold elsewhere, or is still held."""
    position = _two_contracts(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(
        stop, state=OrderState.FILLED, filled=1, avg_price=D(140)
    )
    _, found = stack.manager._actual_exit(position)
    assert not found
    assert stack.manager._sold_in_parts(position) is not None


def test_one_sell_of_the_whole_position_is_still_booked_at_its_price(stack: Stack) -> None:
    """The control: one sell, FILLED, the whole position, at a reported price."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(
        stop, state=OrderState.FILLED, filled=1, avg_price=D(140)
    )
    assert stack.manager._actual_exit(position) == (D(140), True)
    assert stack.manager._sold_in_parts(position) is None


def test_one_sell_filled_with_its_price_pending_is_not_called_sold_in_parts(
    stack: Stack,
) -> None:
    """The control for the alarm: one FILLED sell of the whole position, price not
    reported, is not "sold in parts" (it is the ordinary price-pending case)."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(stop, state=OrderState.FILLED, filled=1, avg_price=None)
    assert stack.manager._actual_exit(position)[1] is False
    assert stack.manager._sold_in_parts(position) is None


# --- the same for reconcile's own orders (`_unpriced_reconcile_order`) -------------------


def test_a_reconcile_sell_that_filled_part_with_its_price_pending_is_never_sent_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no position record (as after a restart) and 2 reported held, reconcile
    sells 2; IBKR fills 1, cancels the rest, and the price and the new holding have not
    arrived. Before: not FILLED, so not a sale: the next settled round sold 2 again.
    Now: one sell, never a second."""
    position = filled_position(stack)
    contract = position.contract_id
    lag = Lag(stack.broker, monkeypatch)
    lag.hold({contract: TWO})
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked.startswith("reconcile-") and status.filled > 0:
            return replace(status, state=OrderState.CANCELLED, filled=1, avg_price=None)
        return status

    monkeypatch.setattr(stack.broker, "status", reported)
    t = SEND + 60 * SEC
    _rounds(stack, None, t, _long_enough(stack))
    sent = [o for o in lag.placed if o.startswith("reconcile-")]
    assert len(sent) == 1
    assert any(h.order_id == sent[0] for h in stack.ledger.sold_unpriced)
    assert len([m for m in _messages(stack) if "CHECK THE FILL" in m]) == 1


def test_a_sell_still_working_is_not_booked_even_with_its_fill_reported(stack: Stack) -> None:
    """Only a FILLED sell books the position (reviewer-style mutant M7): one IBKR still
    reports working, even with the whole position reported filled at a price, is not
    booked yet; the next step sees it FILLED."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(
        stop, state=OrderState.WORKING, filled=1, avg_price=D(140)
    )
    assert stack.manager._actual_exit(position)[1] is False


def test_nothing_sold_is_not_called_sold_in_parts(stack: Stack) -> None:
    """With no sell that sold anything, the alarm keeps its old wording (mutant M9)."""
    position = filled_position(stack)
    assert stack.manager._sold_in_parts(position) is None
