"""The exit never takes the stop down blind, and a stop that will not cancel cannot
hold the exit up for ever."""

from datetime import timedelta

import pytest

from footprint.execution.management.malik import ExitReason, NeedsReconcile
from footprint.execution.orders import OrderState, OrderStatus, PositionsUncertain
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


def test_positions_merely_uncertain_wait_without_halting(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)

    def catching_up() -> dict[str, int]:
        raise PositionsUncertain("the report is a moment behind a fill")

    monkeypatch.setattr(stack.broker, "positions", catching_up)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING
    assert not stack.ledger.account(t).kill_switch_latched  # a wait, not a failure


def test_a_stop_cancelled_at_the_broker_is_noticed_and_the_position_flattened(
    stack: Stack,
) -> None:
    position = filled_position(stack)
    stack.broker.cancel(f"{position.position_id}-stop")  # by hand, or by the broker
    t = SEND + 60 * SEC
    closed = stack.step(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.UNPROTECTED
    assert stack.broker.positions() == {}
    assert any("unprotected" in a.message for a in stack.alarms)


def test_reconcile_waits_on_uncertain_positions_without_halting(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    real = stack.broker.positions
    uncertain = {"left": 1}

    def catching_up() -> dict[str, int]:
        if uncertain["left"]:
            uncertain["left"] -= 1
            raise PositionsUncertain("a moment behind")
        return real()

    monkeypatch.setattr(stack.broker, "positions", catching_up)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert not stack.manager.reconcile(position, t)  # not settled yet
    assert stack.manager.reconcile(position, t + SEC)  # agreed
    assert not stack.halt.tripped
    assert not stack.ledger.account(t).kill_switch_latched


def uncertain_on(stack: Stack, monkeypatch: pytest.MonkeyPatch, *reads: int) -> None:
    """positions() raises PositionsUncertain on the given (1-based) reads."""
    real = stack.broker.positions
    count = {"n": 0}

    def reading() -> dict[str, int]:
        count["n"] += 1
        if count["n"] in reads:
            raise PositionsUncertain("a moment behind")
        return real()

    monkeypatch.setattr(stack.broker, "positions", reading)


def test_an_uncertain_held_read_at_the_exit_waits_then_exits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    uncertain_on(stack, monkeypatch, 2)  # the pre-check reads, the held read does not
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # waits: nothing sent, no halt
    assert position.exit_order_id is None
    assert not stack.ledger.account(t).kill_switch_latched
    closed = stack.manager.flatten(position, t + SEC)
    assert closed is not None
    assert stack.broker.positions() == {}


def test_reconcile_waits_when_what_is_held_after_its_cancels_is_uncertain(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled_position(stack)  # held; reconcile(None) must flatten it
    settle = stack.manager._p.execution.reconcile.settle_seconds
    uncertain_on(stack, monkeypatch, 3)  # the read after its cancels
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert not stack.manager.reconcile(None, t)  # settling the disagreement
    assert not stack.manager.reconcile(None, t + settle * SEC)  # acts; then cannot read
    assert stack.broker.positions() == {stack.contract: 1}  # nothing sold on it
