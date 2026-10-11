"""Grind review of 3e2e040 (PR #21, claude/grind-review-3e2e040.md), G4-2 (LOW, test
gap): reviewer mutant R2, an unknown candidate id stops the search for a sell that
sold with its price pending (`except KeyError: continue` -> `return`), survived.

Reachable: `NeverPlaced` is a KeyError, and a position whose stop never rested has
candidates (`<pid>-stop`) the broker never received. Those come before its exits and
reconcile orders in `_sell_candidates`, so stopping at them would miss a reconcile
sell that already sold, and reconcile would sell beside it (a short). Since G7-1 the
loop lives in `_sells_that_sold`; the property is the same."""

from dataclasses import replace

import pytest

from footprint.execution.orders import NeverPlaced, OrderState, OrderStatus
from tests.execution.conftest import Stack
from tests.execution.test_execution import filled_position


def test_a_stop_the_broker_never_received_does_not_hide_a_later_unpriced_sell(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    pid, stop_id = position.position_id, position.stop_order_id
    assert stop_id is not None
    stop = stack.broker.status(stop_id).order
    sell_id = f"reconcile-{pid}-{position.contract_id}"
    stack.manager._reconcile_orders[pid] = [sell_id]
    never = {stop_id, f"{pid}-stop"}
    real = stack.broker.status

    def answer(asked: str) -> OrderStatus:
        if asked in never:
            raise NeverPlaced(asked)  # the stop never rested at the broker
        if asked == sell_id:
            order = replace(stop, order_id=sell_id, quantity=position.contracts)
            return OrderStatus(order, OrderState.FILLED, position.contracts, None)
        return real(asked)

    monkeypatch.setattr(stack.broker, "status", answer)
    assert stack.manager._unpriced_sell(position) == sell_id
    sold = stack.manager._sells_that_sold(position)
    assert [s.order.order_id for s in sold] == [sell_id]


def test_a_reconcile_order_never_received_does_not_hide_an_older_one_that_sold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule for reconcile orders under any owner (`_unpriced_reconcile_order`,
    the stray and no-position case): the newest one the broker never received is
    skipped, and the older one that sold with its price pending is still found."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    stop = stack.broker.status(stop_id).order
    contract = position.contract_id
    older, newer = f"reconcile-1-x-{contract}", f"reconcile-2-x-{contract}"
    stack.manager._reconcile_orders[None] = [older, newer]
    real = stack.broker.status

    def answer(asked: str) -> OrderStatus:
        if asked == newer:
            raise NeverPlaced(asked)
        if asked == older:
            order = replace(stop, order_id=older)
            return OrderStatus(order, OrderState.CANCELLED, order.quantity, None)
        return real(asked)

    monkeypatch.setattr(stack.broker, "status", answer)
    assert stack.manager._unpriced_reconcile_order(contract) == older
