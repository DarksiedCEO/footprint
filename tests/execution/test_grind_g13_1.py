"""Grind review of 0ee953e (PR #21, claude/grind-review-0ee953e.md), G13-1 (LOW, text):
since G12-1 the id to book an unrecorded round trip under is named when the first
reconcile order for it is sent. When an order still working after reconcile's cancels
(its cancel not confirmed) already covers the holding, no reconcile order is sent, so
no id was ever named, yet the "never recorded" alarm pointed at one.

Now that covering order is named instead, once per contract, with the same one rule:
book the round trip once, under that id, unless a later alarm names a hold under a
reconcile order (then under that one). Text only; nothing sent or booked changes."""

from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.execution.orders import Order, OrderType, Side
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack

STUCK = "stuck-sell"


def _covered_by_a_stuck_sell(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    """Held at the broker, unknown to the ledger, and covered by a sell whose cancel is
    never confirmed (it rests far above the market)."""
    stack.broker._positions[stack.contract] = 1
    stuck = Order(STUCK, stack.contract, Side.SELL, 1, OrderType.LIMIT, Decimal(999))
    stack.broker.place(stuck)
    real_cancel = stack.broker.cancel
    monkeypatch.setattr(
        stack.broker,
        "cancel",
        lambda order_id: (
            stack.broker.status(order_id) if order_id == STUCK else real_cancel(order_id)
        ),
    )
    monkeypatch.setattr(stack.broker, "modify", lambda *args: stack.broker.status(args[0]))


def _run(stack: Stack, rounds: int) -> None:
    for second in range(rounds):
        t = DECISION_TIME + second * timedelta(seconds=1)
        stack.quote(160, 161, t)
        stack.manager.reconcile(None, t)


def _naming(stack: Stack) -> list[str]:
    return [
        a.message
        for a in stack.alarms
        if "no position in the ledger" in a.message and "book_by_hand as" in a.message
    ]


def test_an_unrecorded_holding_covered_by_a_stuck_order_names_that_order_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _covered_by_a_stuck_sell(stack, monkeypatch)
    settle = stack.profiles.execution.reconcile.settle_seconds
    _run(stack, 4 * settle + 4)  # many rounds: named once, not every round
    assert not any(o.startswith("reconcile-") for o in stack.broker._orders)  # covered
    named = _naming(stack)
    assert len(named) == 1, [a.message for a in stack.alarms]
    assert named[0].startswith(f"{STUCK} ")
    assert stack.contract in named[0]
    assert "no reconcile order is sent for it" in named[0]
    assert f"once, with Ledger.book_by_hand as {STUCK}," in named[0]
    assert "never book it twice" in named[0]
    unrecorded = [a.message for a in stack.alarms if a.message.startswith("flattening")]
    assert len(unrecorded) == 1
    assert "if an order already working covers it" in unrecorded[0]  # points at this alarm


def test_a_holding_the_ledger_did_record_covered_by_a_stuck_order_is_never_named(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded in the ledger: it is booked when its position closes, never by hand."""
    from tests.execution.test_execution import filled_position

    position = filled_position(stack)
    stuck = Order(STUCK, position.contract_id, Side.SELL, 1, OrderType.LIMIT, Decimal(999))
    stack.broker.place(stuck)
    real_cancel = stack.broker.cancel
    monkeypatch.setattr(
        stack.broker,
        "cancel",
        lambda order_id: (
            stack.broker.status(order_id) if order_id == STUCK else real_cancel(order_id)
        ),
    )
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(2 * settle + 2):
        t = DECISION_TIME + timedelta(minutes=5) + second * timedelta(seconds=1)
        stack.quote(160, 161, t)
        stack.manager.reconcile(None, t)  # a restart: the recorded position is unknown
    assert _naming(stack) == []
    assert not any("never recorded" in a.message for a in stack.alarms)
