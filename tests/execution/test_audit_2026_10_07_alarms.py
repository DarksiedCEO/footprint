"""Independent audit 2026-10-07 A-4 and A-11: the alarms a position without its stop
raises ring the phone, and each critical marker is pinned at the place that raises it
(not only by its text appearing somewhere in the source)."""

from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.channels import is_critical
from footprint.execution.management.malik import Alarm
from footprint.execution.orders import Order, OrderState, OrderStatus, OrderType, SimulatedBroker
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position
from tests.execution.test_reconcile_hardening import LaggyBroker, use
from tests.execution.test_runner import SEC, open_trade, runner_for


def _said(alarms: list[Alarm], text: str) -> list[str]:
    found = [a.message for a in alarms if text in a.message]
    assert found, f"no alarm containing {text!r}"
    return found


def test_a_fill_without_its_stop_rings_the_phone(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    class FillsAtOnceRefusesStops(SimulatedBroker):
        def place(self, order: Order) -> OrderStatus:
            if order.order_type is OrderType.STOP_LIMIT:
                return OrderStatus(order, OrderState.REJECTED, 0, None)
            status = super().place(order)
            if order.order_id.endswith("-entry"):
                self._positions[order.contract_id] = order.quantity
                status = OrderStatus(order, OrderState.FILLED, order.quantity, order.limit_price)
                self._orders[order.order_id] = status
            return status

        def place_attached(self, parent: Order, child: Order) -> tuple[OrderStatus, OrderStatus]:
            refused = OrderStatus(child, OrderState.REJECTED, 0, None)
            self._orders[child.order_id] = refused
            return self.place(parent), refused

    broker = FillsAtOnceRefusesStops(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    stack.use_broker(broker)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)
    for message in [
        *_said(alarms, "filled without a resting stop"),
        *_said(alarms, "flattening (UNPROTECTED)"),
    ]:
        assert is_critical(message), message


def test_the_failsafe_flatten_rings_the_phone(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def broken(*args: object) -> None:
        raise OSError("stop keeper broken")

    monkeypatch.setattr(stack.stops, "check", broken)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    t = fill
    while (t - fill).total_seconds() <= limit:
        t += SEC
        stack.quote(150, 152, t)
        runner.step(t)
    for message in _said(alarms, "unmanaged since"):
        assert is_critical(message), message


def test_a_report_that_will_not_settle_rings_the_phone(stack: Stack) -> None:
    broker = LaggyBroker(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    use(stack, broker)
    filled_position(stack)
    broker.flicker = True
    t = FILLED_AT + 60 * SEC
    for k in range(stack.profiles.execution.reconcile.max_settle_seconds + 20):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    for message in _said(stack.alarms, "NOT SETTLING"):
        assert is_critical(message), message


def test_a_kill_switch_latch_that_cannot_be_saved_rings_the_phone(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(reason: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stack.ledger, "latch_kill_switch", disk_full)
    stack.manager._stand_down(FILLED_AT, "test")
    assert stack.halt.tripped
    for message in _said(stack.alarms, "latch"):
        assert is_critical(message), message


# --- A-7: reconcile's own sells are one-cancels-all, so a duplicate copy cannot fill twice


def test_reconcile_flatten_orders_carry_a_one_cancels_all_group(stack: Stack) -> None:
    from tests.execution.test_reconcile_hardening import reconcile_orders

    filled_position(stack)
    t = FILLED_AT + 60 * SEC
    for k in range(6):  # held still long enough, then sold
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    placed = reconcile_orders(stack)
    assert placed  # reconcile sold what nobody manages
    for status in placed:  # each in its own group: its copies can only fill once
        assert status.order.oca_group == status.order.order_id
