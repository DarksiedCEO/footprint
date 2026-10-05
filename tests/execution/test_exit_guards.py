"""The exit never takes the stop down blind, and a stop that will not cancel cannot
hold the exit up for ever."""

from datetime import timedelta

import pytest

from footprint.execution.management.malik import NeedsReconcile
from footprint.execution.orders import OrderState, OrderStatus
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position

SEC = timedelta(seconds=1)


def test_a_broker_in_doubt_about_positions_keeps_the_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)

    def in_doubt() -> dict[str, int]:
        raise ConnectionError("positions in doubt")

    monkeypatch.setattr(stack.broker, "positions", in_doubt)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None
    stop = stack.broker.status(f"{position.position_id}-stop")
    assert stop.state is OrderState.WORKING  # still protected
    assert position.stop_order_id is not None
    assert position.exit_order_id is None  # nothing sent beside it


def test_a_stop_that_will_not_cancel_hands_over_to_reconcile(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    stop_id = f"{position.position_id}-stop"

    def refuses(order_id: str) -> OrderStatus:
        return stack.broker.status(order_id)  # IBKR never confirms it

    monkeypatch.setattr(stack.broker, "cancel", refuses)
    escalate = stack.manager._p.execution.reconcile.cancel_escalate_seconds
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # waits, alarmed
    later = t + (escalate - 1) * SEC
    assert stack.manager.flatten(position, later) is None  # still within the window
    with pytest.raises(NeedsReconcile, match="will not cancel"):
        stack.manager.flatten(position, t + escalate * SEC)
    assert stack.broker.status(stop_id).state is OrderState.WORKING
    assert position.exit_order_id is None  # never an exit beside a live stop
    assert stack.ledger.account(t).kill_switch_latched
