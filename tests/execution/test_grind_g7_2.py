"""Grind review of cc98ec0 (PR #21, claude/grind-review-cc98ec0.md), G7-2 (LOW, latent
at `max_contracts: 1`; fail-closed).

On the fill, `Executor._adopt_attached` halts and reports UNPROTECTED for an attached
stop that has sold part of the position, whether that stop has ended or is still
WORKING and protecting. The reviewer asked which is meant; nothing pinned it (reviewer
mutant R1, `and held.state is not OrderState.WORKING`, survived), and the comment and
halt text spoke only of a stop that had ended.

Decided (fail-closed; recorded in docs/decisions-pending.md): a stop that is still
live but has already sold part is NOT adopted as the position's protection. Adopting
it would leave management tracking a size the broker no longer holds; the next steps
(moving the stop, or the size check that cancels it and places a new one for the
whole fill) would act on a stop that already sold. It is kept as the position's stop
(never cancelled or moved here, nothing placed beside it), the halt says it is still
live, and management gets out through its cancel and hands the sale to reconcile
(see the last test). Manager level: config refuses `max_contracts` above 1."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.execution.management.malik import NeedsReconcile
from footprint.execution.orders import Order, OrderState, OrderStatus
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_partly_filled_stop import _placed, _ticket

D = Decimal
SEC = timedelta(seconds=1)
TWO = 2


def _live_stop_sold_one_of_two(stack: Stack, monkeypatch: pytest.MonkeyPatch, stop_id: str) -> None:
    """The broker reports the stop still WORKING (protecting) after it sold 1 of 2."""
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked == stop_id:
            order = replace(status.order, quantity=TWO)
            return replace(status, order=order, filled=1, avg_price=D(140))
        return status

    monkeypatch.setattr(stack.broker, "status", reported)


def test_a_live_stop_that_sold_part_is_kept_but_not_trusted_on_the_fill(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, stop_id = _ticket(stack)
    _live_stop_sold_one_of_two(stack, monkeypatch, stop_id)
    assert stack.broker.status(stop_id).state is OrderState.WORKING
    assert stack.broker.status(stop_id).protects
    sent: list[str] = []
    for name in ("place", "cancel", "modify"):
        real = getattr(stack.broker, name)

        def spy(*args: object, _real: object = real, _name: str = name) -> object:
            sent.append(_name)
            return _real(*args)  # type: ignore[operator]

        monkeypatch.setattr(stack.broker, name, spy)
    spec = stack.stops.preview(D(151), 1)
    order, protected = stack.executor._adopt_attached(stop_id, spec, filled=TWO)
    assert order is not None
    assert order.order_id == stop_id  # management sees what sold
    assert not protected  # never trusted as the position's protection
    assert sent == []  # not moved, not cancelled, nothing beside it
    assert not _placed(stack, f"{ticket.position_id}-stop-2")
    (reason,) = stack.halt.reasons
    assert stop_id in reason
    assert "sold 1 of 2" in reason
    assert "still live" in reason
    assert "by hand" in reason


def test_a_finished_stop_that_sold_part_is_not_called_live(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The halt text tells the two apart (the G6-1 case keeps its wording)."""
    _, stop_id = _ticket(stack)
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked == stop_id:
            order = replace(status.order, quantity=TWO)
            return replace(status, order=order, state=OrderState.CANCELLED, filled=1)
        return status

    monkeypatch.setattr(stack.broker, "status", reported)
    spec = stack.stops.preview(D(151), 1)
    _, protected = stack.executor._adopt_attached(stop_id, spec, filled=TWO)
    assert not protected
    (reason,) = stack.halt.reasons
    assert "sold 1 of 2 and is CANCELLED" in reason
    assert "still live" not in reason


def test_management_gets_out_of_a_live_stop_that_sold_part_through_reconcile(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What follows UNPROTECTED: `flatten` cancels the live stop and hands the sale to
    reconcile; nothing is sold beside the stop."""
    position = filled_position(stack)
    position.contracts = TWO
    stop_id = position.stop_order_id
    assert stop_id is not None
    _live_stop_sold_one_of_two(stack, monkeypatch, stop_id)
    sells: list[str] = []
    place = stack.broker.place

    def placing(order: Order) -> OrderStatus:
        sells.append(order.order_id)
        return place(order)

    monkeypatch.setattr(stack.broker, "place", placing)
    t = SEND + 60 * SEC
    stack.quote(150, 152, t)
    with pytest.raises(NeedsReconcile, match="filled 1 before its cancel"):
        stack.manager.flatten(position, t)
    assert sells == []
    assert stack.ledger.open_position_id == position.position_id
