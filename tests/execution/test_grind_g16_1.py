"""Grind review of ba5f2d5 (PR #21, claude/grind-review-ba5f2d5.md), G16-1 (LOW): when
two stops of one position both fill (sold twice), management's alarm told Andre to
"flatten by hand", while reconcile then buys the short back by itself (N6/F2: a short is
seen and bought back). Andre acting on the alarm while reconcile's buy works would buy
twice and leave the account long with no resting stop.

Now the alarm says what really happens: reconcile buys back the short by itself; Andre
does not flatten by hand until it reports flat, then books the position by hand. And
reconcile's behaviour after sold twice is pinned: one buy of the short quantity, never a
sell, nothing booked (the ledger keeps the position for Ledger.book_by_hand), the halt
kept."""

from datetime import timedelta

import pytest

from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import NeedsReconcile
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT
from tests.execution.test_grind_g6_5 import _lost_its_stop_id, _orphan

SEC = timedelta(seconds=1)


def _sold_twice(stack: Stack) -> Position:
    """Two stops of one position (each in its own group, to build the broken state)
    both fill; management sees it and hands over to reconcile."""
    position = _lost_its_stop_id(stack)
    pid = position.position_id
    _orphan(stack, position, f"{pid}-stop-2", own_group=True)
    _orphan(stack, position, f"{pid}-stop-wait", own_group=True)
    t = FILLED_AT + 5 * SEC
    stack.quote(110, 111, t)
    with pytest.raises(NeedsReconcile):
        stack.step(position, stack.quote(110, 111, t), (), t)
    return position


def test_the_sold_twice_alarm_never_asks_for_a_flatten_by_hand_beside_reconcile(
    stack: Stack,
) -> None:
    position = _sold_twice(stack)
    [alarm] = [a for a in stack.alarms if "sold twice" in a.message]
    assert position.position_id in alarm.message
    assert "flatten by hand" not in alarm.message
    assert "buys back" in alarm.message
    assert "until it reports flat" in alarm.message
    assert "Ledger.book_by_hand" in alarm.message


def test_after_sold_twice_reconcile_only_buys_back_the_short_and_books_nothing(
    stack: Stack,
) -> None:
    position = _sold_twice(stack)
    contract = position.contract_id
    assert stack.broker.positions() == {contract: -position.contracts}  # short
    before = len(stack.broker.log)
    t = FILLED_AT + 5 * SEC
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(3 * settle + 2):
        stack.quote(110, 111, t + second * SEC)
        stack.manager.reconcile(position, t + second * SEC)
    placed = [line for line in stack.broker.log[before:] if line.startswith("placed")]
    assert len(placed) == 1
    assert placed[0].endswith("LIMIT BUY")  # never a market order, never a sell
    assert stack.broker.positions() == {}  # flat: the short is bought back, not more
    assert stack.ledger.open_position_id == position.position_id  # booked by hand only
    assert stack.halt.tripped
    assert any(
        "sold in parts" in a.message and "Ledger.book_by_hand" in a.message for a in stack.alarms
    )
