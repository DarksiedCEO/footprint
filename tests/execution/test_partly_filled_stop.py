"""Grind review of 8ea3ad1 (PR #21, claude/grind-review-8ea3ad1.md).

G6-1: a stop that sold part of the position and then ended (cancelled by hand or by
IBKR) is not "finished without filling". A new stop sized from the entry's fill would
sell what the old one already sold: a short. So, wherever such a stop is found, nothing
is placed beside it, the system halts, and Andre is told how much it sold:
- while the entry waits for its price (Executor._guard_pending_stop);
- when the fill is adopted (Executor._adopt_attached): the position keeps the old stop,
  so management sees it;
- in management (Manager.manage): a stop gone at the broker that sold part of the
  position hands over to reconcile, never to an exit sized from a stale position.

The replacement placed while the price is pending is sized and priced for what was
filled, never for the plan or the attached stop's own size (reviewer mutants R3, R4).

G6-2: a replacement refused before it reaches the broker (NotSent: nothing exists there)
gives the ticket back its old stop id, so the next step tries again, instead of asking
for ever about an order the broker never received. An outcome that is NOT known (the
send failed and may have arrived) keeps the replacement's id (reviewer mutant R1)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.executor.diego import EntryOutcome, EntryTicket
from footprint.execution.management.malik import NeedsReconcile
from footprint.execution.orders import (
    NeverPlaced,
    NotSent,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_pending_entry_stop import _broker_cancels_the_stop, _replacements
from tests.execution.test_reconcile_unpriced_entry import Scene

D = Decimal
SEC = timedelta(seconds=1)


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _ticket(stack: Stack) -> tuple[EntryTicket, str]:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.stop_order_id is not None
    return ticket, ticket.stop_order_id


def _report(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    order_id: str,
    *,
    filled: int,
    quantity: int,
) -> None:
    """The broker reports `order_id` finished, `filled` of `quantity` sold."""
    SimulatedBroker.cancel(stack.broker, order_id)
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked == order_id:
            order = replace(status.order, quantity=quantity)
            return replace(status, order=order, state=OrderState.CANCELLED, filled=filled)
        return status

    monkeypatch.setattr(stack.broker, "status", reported)


def _placed(stack: Stack, order_id: str) -> bool:
    return order_id in stack.broker._orders


# --- G6-1: never a stop beside one that already sold part of the position -----------------


def test_a_partly_filled_stop_during_the_price_wait_gets_nothing_beside_it(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe P2: 2 held, the attached stop sold 1 and was then cancelled. A stop for 2
    would sell 1 more than is held."""
    ticket, stop_id = _ticket(stack)
    pid = ticket.position_id
    _report(stack, monkeypatch, stop_id, filled=1, quantity=2)
    for _ in range(3):
        stack.executor._guard_pending_stop(ticket, 2)
    assert not _placed(stack, f"{pid}-stop-wait")
    assert ticket.stop_order_id == stop_id  # still asked about: it is what sold
    assert stack.halt.tripped
    (reason,) = stack.halt.reasons  # once, never one per step
    assert stop_id in reason
    assert "sold 1 of 2" in reason
    assert "by hand" in reason


def test_a_partly_filled_attached_stop_is_kept_on_the_fill_never_doubled(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, stop_id = _ticket(stack)
    _report(stack, monkeypatch, stop_id, filled=1, quantity=2)
    spec = stack.stops.preview(D(151), 1)
    order, protected = stack.executor._adopt_attached(stop_id, spec, filled=2)
    assert order is not None
    assert order.order_id == stop_id  # management sees what sold
    assert not protected
    assert not _placed(stack, f"{ticket.position_id}-stop-2")
    assert stack.halt.tripped
    (reason,) = stack.halt.reasons
    assert stop_id in reason
    assert "sold 1 of 2" in reason


def test_the_fill_path_keeps_a_partly_filled_stop_as_the_positions_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through Executor._after: the position is UNPROTECTED, keeps the old stop, and no
    second stop is placed (the old code placed `-stop-2` for the whole fill)."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    stop_id = f"{ticket.position_id}-stop"
    stack.quote(150, D(151), SEND + SEC)  # the entry fills at 151
    assert stack.broker.status(ticket.order_id).state is OrderState.FILLED
    _report(stack, monkeypatch, stop_id, filled=1, quantity=2)
    ticket = stack.executor.on_time(ticket, SEND + 2 * SEC)
    assert ticket.position is not None
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position.stop_order_id == stop_id
    assert not _placed(stack, f"{ticket.position_id}-stop-2")
    assert stack.halt.tripped


def test_management_hands_a_stop_that_sold_part_and_ended_to_reconcile(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held stop cancelled at IBKR after it sold part: the exit must not be sized from
    a position read that may not show that sale yet, and the sale is never booked at the
    exit's price. Reconcile takes over, as for a stop partly filled before our cancel."""
    position = filled_position(stack)
    assert position.stop_order_id is not None
    stop_id = position.stop_order_id
    _report(stack, monkeypatch, stop_id, filled=1, quantity=2)
    t = SEND + 60 * SEC
    stack.quote(150, 152, t)
    with pytest.raises(NeedsReconcile, match="sold 1 of 2"):
        stack.manager.manage(position, (), t)
    assert position.exit_order_id is None  # nothing sent
    assert not [o for o in stack.broker._orders if "-exit-" in o]
    assert stack.ledger.account(t).kill_switch_latched
    assert any(stop_id in a.message and "sold 1 of 2" in a.message for a in stack.alarms)


def test_management_still_flattens_a_stop_that_ended_with_nothing_sold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: cancelled by hand with nothing sold is the old D38 path (flatten)."""
    position = filled_position(stack)
    assert position.stop_order_id is not None
    _report(stack, monkeypatch, position.stop_order_id, filled=0, quantity=1)
    t = SEND + 60 * SEC
    stack.quote(150, 152, t)
    stack.manager.manage(position, (), t)
    assert position.exit_order_id is not None
    assert not stack.ledger.account(t).kill_switch_latched


# --- R3, R4: the replacement is for what was filled ---------------------------------------


def test_the_replacement_is_sized_from_the_fill_not_the_attached_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 planned (the attached stop is for 2), 1 filled, the entry's rest cancelled and
    the stop cancelled with nothing sold: the new stop is for 1 (R3)."""
    ticket, stop_id = _ticket(stack)
    _report(stack, monkeypatch, stop_id, filled=0, quantity=2)
    stack.executor._guard_pending_stop(ticket, 1)
    new = stack.broker.status(f"{ticket.position_id}-stop-wait").order
    assert new.quantity == 1
    spec = stack.stops.preview(ticket.limit, 1)
    assert (new.stop_price, new.limit_price) == (spec.trigger, spec.limit)
    assert (new.side, new.order_type) == (Side.SELL, OrderType.STOP_LIMIT)
    assert not stack.halt.tripped


def test_the_replacement_is_priced_for_the_fill_not_the_plan(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan was for more than filled: the stop is priced for what is held (R4).
    (Priced for the plan here would even be refused: above max_contracts.)"""
    ticket, stop_id = _ticket(stack)
    plan = replace(ticket.approved.plan, contracts=ticket.approved.plan.contracts + 1)
    theta = replace(ticket.approved.theta, contracts=plan.contracts)  # judged the same plan
    ticket.approved = replace(ticket.approved, plan=plan, theta=theta)
    _report(stack, monkeypatch, stop_id, filled=0, quantity=plan.contracts)
    stack.executor._guard_pending_stop(ticket, 1)
    new = stack.broker.status(f"{ticket.position_id}-stop-wait").order
    spec = stack.stops.preview(ticket.limit, 1)
    assert (new.quantity, new.stop_price, new.limit_price) == (1, spec.trigger, spec.limit)


# --- G6-2: a replacement that never reached the broker is tried again ---------------------


def test_a_replacement_refused_before_the_broker_is_tried_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, stop_id = _ticket(stack)
    replacement = f"{ticket.position_id}-stop-wait"
    _report(stack, monkeypatch, stop_id, filled=0, quantity=1)
    real_place = stack.broker.place
    refused: list[str] = []

    def not_ready_once(order: Order) -> OrderStatus:
        if order.order_id == replacement and not refused:
            refused.append(order.order_id)
            raise NotSent("not ready")
        return real_place(order)

    monkeypatch.setattr(stack.broker, "place", not_ready_once)
    with pytest.raises(NotSent):
        stack.executor._guard_pending_stop(ticket, 1)
    assert ticket.stop_order_id == stop_id  # never an id the broker never received
    with pytest.raises(NeverPlaced):
        stack.executor._broker.status(replacement)
    stack.executor._guard_pending_stop(ticket, 1)  # the next step tries again
    assert ticket.stop_order_id == replacement
    assert stack.executor._broker.status(replacement).protects
    assert not stack.halt.tripped


def test_a_replacement_whose_send_outcome_is_unknown_keeps_its_id(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It may have reached the broker: the ticket asks about it from now on and never
    places a second one beside it (R1: recorded before the send)."""
    ticket, stop_id = _ticket(stack)
    replacement = f"{ticket.position_id}-stop-wait"
    _report(stack, monkeypatch, stop_id, filled=0, quantity=1)
    real_place = stack.broker.place

    def lost(order: Order) -> OrderStatus:
        if order.order_id == replacement:
            raise TimeoutError("no answer")
        return real_place(order)

    monkeypatch.setattr(stack.broker, "place", lost)
    with pytest.raises(TimeoutError):
        stack.executor._guard_pending_stop(ticket, 1)
    assert ticket.stop_order_id == replacement
    assert replacement in stack.executor._broker.unfinished  # reconcile will find it


def test_runner_retries_a_replacement_refused_before_the_broker(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe P1 at the runner level: the first replacement send is refused
    before the broker. The next step places it; the position ends protected by it, and
    nothing ever asks about an order the broker never received."""
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    placing = scene.stack.broker.place
    refused: list[str] = []

    def not_ready_once(order: Order) -> OrderStatus:
        if order.order_id.endswith("-stop-wait") and not refused:
            refused.append(order.order_id)
            raise NotSent("not ready")
        return placing(order)

    monkeypatch.setattr(scene.stack.broker, "place", not_ready_once)
    _broker_cancels_the_stop(scene)
    scene.step(3)
    assert refused
    (new_id,) = _replacements(scene)
    assert scene.stack.broker.status(new_id).protects
    assert not any("NeverPlaced" in m for m in scene.messages())
    assert scene.lag.sells() == []
    assert SimulatedBroker.positions(scene.stack.broker) == {scene.stack.contract: 1}
