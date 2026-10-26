"""Independent delta review of f44b0e4 (claude/audit-2026-10-07-delta.md), F-2: every
place an exit can meet a lost broker treats it as an outage (BrokerDown reaches the
runner, which waits and reconciles), never as a fault that halts and latches the day."""

from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.execution.orders import BrokerDown, Order, OrderStatus, OrderType, Side
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import exit_rests_on_arrival, filled_position

SEC = timedelta(seconds=1)


def test_a_lost_broker_while_sweeping_the_contract_is_an_outage(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site A6a: cancelling the other working orders on the contract at an exit's start."""
    position = filled_position(stack)
    stray = Order("stray-1", position.contract_id, Side.SELL, 1, OrderType.LIMIT, Decimal(300))
    stack.manager._broker.place(stray)  # another working sell on the contract
    original = stack.broker.cancel

    def drops_on_the_stray(order_id: str) -> OrderStatus:
        if order_id == "stray-1":
            raise BrokerDown("IB Gateway restarting")
        return original(order_id)

    monkeypatch.setattr(stack.broker, "cancel", drops_on_the_stray)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    with pytest.raises(BrokerDown):
        stack.manager.flatten(position, t)
    assert not stack.halt.tripped
    assert not stack.ledger.account(t).kill_switch_latched


def test_a_lost_broker_while_working_an_exit_is_an_outage(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site A6c: an exit already working when the broker goes away."""
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # the exit rests
    assert position.exit_order_id is not None

    def gone(order_id: str) -> OrderStatus:
        raise BrokerDown("IB Gateway restarting")

    monkeypatch.setattr(stack.broker, "status", gone)
    later = t + 5 * SEC
    stack.quote(150, 151, later)
    with pytest.raises(BrokerDown):
        stack.manager.flatten(position, later)
    assert not stack.halt.tripped
    assert not stack.ledger.account(later).kill_switch_latched
    assert position.exit_order_id is not None  # still followed once IBKR is back


def test_reconcile_order_ids_and_groups_never_repeat_across_restarts(stack: Stack) -> None:
    """F-7: a restarted process starts its reconcile rounds at 0 again; its orders (and
    their one-cancels-all groups, named after them) must still be new names."""
    from footprint.execution.management.malik import Manager
    from tests.execution.test_reconcile_hardening import reconcile_orders

    filled_position(stack)
    t = SEND + 60 * SEC
    for k in range(6):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    placed = [s.order.order_id for s in reconcile_orders(stack)]
    assert placed
    first = stack.manager
    restarted = Manager(
        first._p, first._broker, first._stops, first._ledger, first._halt, first._book
    )
    token = first._run_token
    assert token != restarted._run_token  # a new process, a new name for every round
    assert all(token in order_id for order_id in placed)
