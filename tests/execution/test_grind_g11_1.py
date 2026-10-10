"""Grind review of a893626 (PR #21, claude/grind-review-a893626.md), G11-1 (LOW, latent
at `max_contracts: 1`; the G7-1 class on the `_await_price` path).

G7-1 made reconcile book a position only when one sell, FILLED at a reported price,
sold all of it. `_await_price` (management and `flatten` waiting on a sell whose price
was pending) did not: it booked the WHOLE position at that one order's price as soon
as the price arrived. The reviewer's probe R1: 2 held, the stop sold 1 at 140 and was
cancelled, the report is right at 1; reconcile sells the 1 and its fill arrives with
the price pending, so that order becomes the position's pending sell; the price then
arrives at 150 and the whole position was booked at 150, one contract at a price it
never got (a guessed price; the loss misstated).

Now `_await_price` books only what `_actual_exit` finds (one sell, FILLED, priced, the
whole position). Otherwise Andre is told each sale, once, the kill switch is latched,
nothing more is sold, and reconcile takes over (which books nothing either).

Manager level, as the reviewer's probe: config refuses `max_contracts` above 1, so the
position is made 2 contracts by hand."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import NeedsReconcile
from footprint.execution.orders import OrderState, OrderStatus, SimulatedBroker
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag, _wait
from tests.execution.test_grind_g7_1 import (
    SEC,
    _hand_over,
    _messages,
    _stop_sold_one_of_two,
    _two_contracts,
)

D = Decimal
ONE = 1


class _ReconcilePriceLag:
    """IBKR reports reconcile's own sell FILLED before its price, until `reveal()`."""

    def __init__(self, stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hiding = True
        real = stack.broker.status

        def reported(asked: str) -> OrderStatus:
            status = real(asked)
            if self.hiding and asked.startswith("reconcile-") and status.filled > 0:
                return replace(status, avg_price=None)
            return status

        monkeypatch.setattr(stack.broker, "status", reported)

    def reveal(self) -> None:
        self.hiding = False


def _reconcile_sells_the_rest_price_pending(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> tuple[Position, Lag, _ReconcilePriceLag, datetime]:
    """R1 up to the point where reconcile's own sell is the position's pending sell."""
    lag = Lag(stack.broker, monkeypatch)
    position = _two_contracts(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag.hold({position.contract_id: ONE})  # the report is right: 1 left after the stop
    _stop_sold_one_of_two(stack, monkeypatch, stop_id, D(140))
    priced = _ReconcilePriceLag(stack, monkeypatch)
    t = _hand_over(stack, position)
    pid = position.position_id
    limit = int(_wait(stack).total_seconds())
    for _ in range(limit):
        t += SEC
        stack.quote(150, 151, t)
        stack.manager.reconcile(position, t)
        if pid in stack.manager._pending:
            break
    assert pid in stack.manager._pending, "reconcile's sell never became the pending sell"
    pending = stack.manager._pending[pid][0]
    assert pending.startswith("reconcile-")
    assert lag.sells() == [pending]  # sold once, the 1 really held
    return position, lag, priced, t


def test_a_position_sold_in_parts_is_never_booked_when_the_last_price_arrives(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe R1 through `manage`. Before: Closed at 150 for both
    contracts. Now: nothing booked, one alarm naming each sale, kill switch latched,
    reconcile takes over; nothing more is sold."""
    position, lag, priced, t = _reconcile_sells_the_rest_price_pending(stack, monkeypatch)
    stop_id = position.stop_order_id
    pending = stack.manager._pending[position.position_id][0]
    priced.reveal()
    lag.reveal()
    t += SEC
    stack.quote(150, 151, t)
    with pytest.raises(NeedsReconcile, match="sold in parts"):
        stack.manager.manage(position, (), t)
    assert stack.ledger.open_position_id == position.position_id  # nothing booked
    assert stack.ledger.account(t).kill_switch_latched
    booked = [m for m in _messages(stack) if "sold in parts" in m]
    assert len(booked) == 1
    assert f"{stop_id} sold 1 of 2" in booked[0]
    assert "at 140" in booked[0]
    assert pending in booked[0]
    assert "at 150" in booked[0]
    assert "Ledger.book_by_hand" in booked[0]
    # Reconcile then agrees it is flat, books nothing and never alarms twice.
    for _ in range(3):
        t += SEC
        stack.quote(150, 151, t)
        stack.manager.reconcile(position, t)
    assert stack.manager.take_reconcile_closes() == []
    assert stack.ledger.open_position_id == position.position_id
    assert len([m for m in _messages(stack) if "sold in parts" in m]) == 1
    assert lag.sells() == [pending]  # never sold again
    assert SimulatedBroker.positions(stack.broker) == {}


def test_flatten_never_books_a_position_sold_in_parts(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same probe through `flatten` (the runner's fail-safe path)."""
    position, lag, priced, t = _reconcile_sells_the_rest_price_pending(stack, monkeypatch)
    priced.reveal()
    lag.reveal()
    # Latched already by the stop's partial fill; cleared here (as Andre may have) so
    # this path's own latch is what is tested.
    stack.ledger.clear_kill_switch("test")
    assert not stack.ledger.account(t).kill_switch_latched
    t += SEC
    stack.quote(150, 151, t)
    with pytest.raises(NeedsReconcile, match="sold in parts"):
        stack.manager.flatten(position, t)
    assert stack.ledger.open_position_id == position.position_id
    assert stack.ledger.account(t).kill_switch_latched
    assert any("sold in parts, book it by hand" in r for r in stack.halt.reasons)
    assert len(lag.sells()) == 1


def test_a_whole_sale_whose_price_was_pending_is_still_booked(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: one sell (the stop), FILLED, all of the position, price pending,
    then reported: booked at that price, as before."""
    lag = Lag(stack.broker, monkeypatch)
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag.hide(stop_id)
    stop = stack.broker._orders[stop_id]
    stack.broker._orders[stop_id] = replace(
        stop, state=OrderState.FILLED, filled=1, avg_price=D(140)
    )
    t = SEND + 60 * SEC
    stack.quote(150, 152, t)
    assert stack.manager.manage(position, (), t) is None  # price pending: waiting
    assert position.position_id in stack.manager._pending
    lag.reveal()
    closed = stack.manager.manage(position, (), t + SEC)
    assert closed is not None
    assert closed.exit_price == D(140)
    assert [m for m in _messages(stack) if "sold in parts" in m] == []
