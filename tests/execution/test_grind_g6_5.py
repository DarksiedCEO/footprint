"""Grind review of 8ea3ad1 (PR #21, claude/grind-review-8ea3ad1.md), G6-5 (INFO, carried
since): the checks for a stop that reached the broker unacknowledged and filled looked
only under `<pid>-stop` (and the position's own stop id). A position's stop can also be
`<pid>-stop-2` (placed on the fill when the attached stop was not working) or
`<pid>-stop-wait` (placed while the entry's price was pending). One of those that filled
after the position lost its stop id was not found: management's orphan check missed the
sale, and reconcile could not book the position at that real fill.

Now every id a position's stop can have is asked about, in the orphan check and among
reconcile's sell candidates. Two stops that both filled are a sale twice: nothing is
booked or sold, reconcile takes over (and books it by hand, as "sold in parts")."""

from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.execution.executor.diego import Position, stop_order_ids
from footprint.execution.management.malik import ExitReason, NeedsReconcile
from footprint.execution.orders import Order, OrderState, OrderType, Side
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position

D = Decimal
SEC = timedelta(seconds=1)


def _lost_its_stop_id(stack: Stack) -> Position:
    """A held position whose own stop is gone (cancelled, nothing sold) and which no
    longer knows a stop id: as after a stop send whose answer was lost."""
    position = filled_position(stack)
    stack.broker.cancel(f"{position.position_id}-stop")
    position.stop_order_id = None
    return position


def _orphan(stack: Stack, position: Position, order_id: str, *, own_group: bool = False) -> None:
    """A stop that reached the broker unacknowledged, resting under `order_id` (in its
    own one-cancels-all group only to build a broken state where two can both fill)."""
    stack.broker.place(
        Order(
            order_id=order_id,
            contract_id=position.contract_id,
            side=Side.SELL,
            quantity=position.contracts,
            order_type=OrderType.STOP_LIMIT,
            limit_price=D(110),
            stop_price=D(120),
            oca_group=order_id if own_group else position.position_id,
        )
    )


def test_every_stop_id_the_executor_makes_is_listed() -> None:
    assert stop_order_ids("p") == ("p-stop", "p-stop-2", "p-stop-wait")


@pytest.mark.parametrize("suffix", ["-stop-2", "-stop-wait"])
def test_an_orphan_stop_under_another_id_that_filled_closes_the_trade(
    stack: Stack, suffix: str
) -> None:
    position = _lost_its_stop_id(stack)
    _orphan(stack, position, f"{position.position_id}{suffix}")
    t = FILLED_AT + 5 * SEC
    stack.quote(110, 111, t)  # the orphan triggers and fills at 110
    assert stack.broker.positions() == {}
    closed = stack.step(position, stack.quote(110, 111, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(110))
    assert not any("-exit-" in line and line.startswith("placed") for line in stack.broker.log)
    assert stack.broker.positions() == {}  # flat, never short


@pytest.mark.parametrize("suffix", ["-stop-2", "-stop-wait"])
def test_reconcile_books_a_flat_position_at_the_fill_of_a_stop_under_another_id(
    stack: Stack, suffix: str
) -> None:
    position = _lost_its_stop_id(stack)
    _orphan(stack, position, f"{position.position_id}{suffix}")
    t = FILLED_AT + 5 * SEC
    stack.quote(110, 111, t)  # filled at 110 at the broker
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(2 * settle + 2):
        stack.quote(110, 111, t + second * SEC)
        stack.manager.reconcile(position, t + second * SEC)
    assert stack.ledger.open_position_id is None  # booked, at the real fill
    assert not any("no one fill confirms" in a.message for a in stack.alarms)
    assert not any(line.startswith("placed reconcile") for line in stack.broker.log)


def test_two_stops_that_both_filled_are_never_booked_or_sold_beside(
    stack: Stack,
) -> None:
    position = _lost_its_stop_id(stack)
    pid = position.position_id
    _orphan(stack, position, f"{pid}-stop-2", own_group=True)
    _orphan(stack, position, f"{pid}-stop-wait", own_group=True)
    t = FILLED_AT + 5 * SEC
    stack.quote(110, 111, t)  # both trigger: sold twice (already a broken state)
    assert stack.broker.status(f"{pid}-stop-2").state is OrderState.FILLED
    assert stack.broker.status(f"{pid}-stop-wait").state is OrderState.FILLED
    with pytest.raises(NeedsReconcile):
        stack.step(position, stack.quote(110, 111, t), (), t)
    assert stack.ledger.open_position_id == pid  # not booked at either price
    assert not any("-exit-" in line and line.startswith("placed") for line in stack.broker.log)
    assert stack.halt.tripped
    assert any("sold twice" in a.message and "by hand" in a.message for a in stack.alarms)


def test_an_orphan_stop_under_another_id_filled_with_its_price_pending_waits_under_its_id(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sold, its price not reported yet: nothing is sent, and the hold names the stop
    that sold (the one whose price is awaited), not `<pid>-stop`."""
    from dataclasses import replace

    position = _lost_its_stop_id(stack)
    stop_2 = f"{position.position_id}-stop-2"
    _orphan(stack, position, stop_2)
    t = FILLED_AT + 5 * SEC
    stack.quote(110, 111, t)  # filled at the broker
    real_status = stack.broker.status
    monkeypatch.setattr(
        stack.broker,
        "status",
        lambda order_id: (
            replace(real_status(order_id), avg_price=None)
            if order_id == stop_2
            else real_status(order_id)
        ),
    )
    assert stack.step(position, stack.quote(110, 111, t), (), t) is None
    assert stack.manager._pending[position.position_id][0] == stop_2
    assert stack.ledger.open_position_id == position.position_id  # nothing guessed
    assert not any("-exit-" in line and line.startswith("placed") for line in stack.broker.log)
