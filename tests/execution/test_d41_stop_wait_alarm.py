"""Decision D41, option (b), signed by Andre 2026-10-25 (grind review G6-3).

While an entry the broker reports FILLED waits for its price, an attached stop that
ends with nothing sold (cancelled by hand at IBKR, or by IBKR) is still replaced by ONE
new stop, `<position>-stop-wait` (N3-5), and Andre now gets ONE alarm per position:
"stop X was cancelled at the broker during the price wait; a new stop Y was placed".
No halt and no flatten. The alarm is never repeated on later steps. A replacement that
does not end up protecting, or whose send outcome is unknown, says so in that one
alarm instead of "placed". Nothing is sent for a stop that was not finished; a stop
that sold part (G6-1) keeps its halt; a replacement refused before the broker (NotSent,
G6-2) is retried, and only the send that reached the broker alarms.

Restart: the entry ticket lives only in the runner's memory (it is not in the ledger's
state file), so a restarted runner has no ticket and never runs the price-wait stop
check for an old entry: the alarm cannot repeat across a restart, and no new persisted
state is needed."""

import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.executor.diego import EntryTicket
from footprint.execution.orders import (
    DuplicateOrderId,
    NotSent,
    Order,
    OrderState,
    OrderStatus,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_pending_entry_stop import _broker_cancels_the_stop, _replacements
from tests.execution.test_reconcile_unpriced_entry import Scene, _past_everything

D = Decimal
SEC = timedelta(seconds=1)
WAIT = "during the price wait"


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _wait_alarms(messages: list[str]) -> list[str]:
    return [m for m in messages if WAIT in m]


def _routed(stack: Stack) -> list[str]:
    sent: list[str] = []
    stack.executor.route_alarms(sent.append)
    return sent


def _ticket(stack: Stack) -> tuple[EntryTicket, str]:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.stop_order_id is not None
    return ticket, ticket.stop_order_id


def _ended(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    order_id: str,
    *,
    state: OrderState = OrderState.CANCELLED,
    filled: int = 0,
    quantity: int = 1,
) -> None:
    """The broker reports `order_id` finished as `state`, `filled` of `quantity` sold."""
    SimulatedBroker.cancel(stack.broker, order_id)
    real = stack.broker.status

    def reported(asked: str) -> OrderStatus:
        status = real(asked)
        if asked == order_id:
            order = replace(status.order, quantity=quantity)
            return replace(status, order=order, state=state, filled=filled)
        return status

    monkeypatch.setattr(stack.broker, "status", reported)


# --- runner level: the alarm reaches Andre's line, once ------------------------------------


def test_a_stop_cancelled_during_the_wait_alarms_once_naming_both_stops(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    assert _wait_alarms(scene.messages()) == []
    _broker_cancels_the_stop(scene)
    scene.step()
    (new_id,) = _replacements(scene)
    (alarm,) = _wait_alarms(scene.messages())
    assert alarm == (
        f"{scene.entry.removesuffix('-entry')}: stop {scene.stop} was cancelled at the "
        f"broker during the price wait; a new stop {new_id} was placed"
    )
    assert not alarm.startswith("HALT")
    assert not scene.stack.halt.tripped  # (b): no halt
    assert scene.lag.sells() == []  # (b): no flatten
    scene.step(5)
    assert len(_wait_alarms(scene.messages())) == 1  # never one per step
    scene.lag.reveal()
    scene.step(3)
    assert scene.runner.position is not None
    assert scene.runner.position.stop_order_id == new_id
    assert len(_wait_alarms(scene.messages())) == 1  # nor on the fill
    assert scene.lag.sells() == []


def test_a_cancel_on_the_settle_path_alarms_once(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    scene.runner.doubt()
    _broker_cancels_the_stop(scene)
    scene.step(4)
    (new_id,) = _replacements(scene)
    (alarm,) = _wait_alarms(scene.messages())
    assert scene.stop in alarm
    assert new_id in alarm
    assert alarm.endswith("was placed")


def test_a_cancel_past_the_wait_alarms_once(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=True)
    scene.step(_past_everything(scene.stack))
    assert _wait_alarms(scene.messages()) == []
    _broker_cancels_the_stop(scene)
    scene.step(3)
    (new_id,) = _replacements(scene)
    (alarm,) = _wait_alarms(scene.messages())
    assert new_id in alarm


def test_no_alarm_while_the_stop_protects(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step(6)
    assert _replacements(scene) == []
    assert _wait_alarms(scene.messages()) == []
    scene.lag.reveal()
    scene.step(2)
    assert scene.runner.position is not None
    assert _wait_alarms(scene.messages()) == []


def test_no_alarm_when_the_stop_filled_during_the_wait(scene: Scene) -> None:
    """A filled stop is a sale, not a cancel: nothing is placed, nothing is alarmed."""
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    stop = scene.stack.broker.status(scene.stop)
    assert stop.order.stop_price is not None
    low = stop.order.stop_price - D(1)
    for _ in range(3):
        scene.t += SEC
        scene.stack.quote(float(low - D(1)), low, scene.t)
        scene.runner.step(scene.t)
    assert scene.stack.broker.status(scene.stop).state is OrderState.FILLED
    assert _replacements(scene) == []
    assert _wait_alarms(scene.messages()) == []


def test_a_refused_replacement_alarms_once_without_saying_placed(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker answers the replacement REJECTED: the one alarm must not claim it was
    placed; the existing "no resting stop ... by hand" halt follows on the next step."""
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    placing = scene.stack.broker.place

    def refusing(order: Order) -> OrderStatus:
        status = placing(order)
        if order.order_id.endswith("-stop-wait"):
            SimulatedBroker.cancel(scene.stack.broker, order.order_id)
            return replace(status, state=OrderState.REJECTED)
        return status

    monkeypatch.setattr(scene.stack.broker, "place", refusing)
    _broker_cancels_the_stop(scene)
    scene.step(5)
    (alarm,) = _wait_alarms(scene.messages())
    assert "was placed" not in alarm
    assert "REJECTED" in alarm
    assert "no stop rests at the broker for it" in alarm
    assert any("no resting stop" in r for r in scene.stack.halt.reasons)  # unchanged


def test_notsent_then_placed_alarms_once_only_for_the_send_that_reached_the_broker(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    scene.step(4)
    assert refused
    (new_id,) = _replacements(scene)
    assert scene.stack.broker.status(new_id).protects  # G6-2 retry unchanged
    (alarm,) = _wait_alarms(scene.messages())
    assert alarm.endswith(f"a new stop {new_id} was placed")


# --- executor level --------------------------------------------------------------------


def test_unit_alarm_once_across_repeated_checks(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id)
    for _ in range(4):
        stack.executor._guard_pending_stop(ticket, 1)
    pid = ticket.position_id
    assert sent == [
        f"{pid}: stop {stop_id} was cancelled at the broker during the price wait; "
        f"a new stop {pid}-stop-wait was placed"
    ]
    assert not stack.halt.tripped


def test_unit_once_per_position_even_if_the_check_reaches_the_send_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the ticket reverted to the attached stop (as NotSent does) after
    a send that DID alarm still alarms only once for that position."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id)
    stack.executor._guard_pending_stop(ticket, 1)
    SimulatedBroker.cancel(stack.broker, f"{ticket.position_id}-stop-wait")
    ticket.stop_order_id = stop_id
    with pytest.raises(DuplicateOrderId):  # ids are never reused at the broker
        stack.executor._guard_pending_stop(ticket, 1)
    assert len(sent) == 1


def test_unit_a_rejected_attached_stop_is_named_as_rejected(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id, state=OrderState.REJECTED)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert f"stop {stop_id} was rejected at the broker during the price wait" in alarm


def test_unit_no_alarm_for_a_partly_filled_stop_its_halt_unchanged(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id, filled=1, quantity=2)
    for _ in range(3):
        stack.executor._guard_pending_stop(ticket, 2)
    assert sent == []
    assert ticket.stop_order_id == stop_id
    assert f"{ticket.position_id}-stop-wait" not in stack.broker._orders
    (reason,) = stack.halt.reasons
    assert "sold 1 of 2" in reason
    assert "by hand" in reason


def test_unit_notsent_sends_no_alarm_and_keeps_g6_2(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    replacement = f"{ticket.position_id}-stop-wait"
    _ended(stack, monkeypatch, stop_id)
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
    assert sent == []  # nothing reached the broker: nothing to report yet
    assert ticket.stop_order_id == stop_id
    stack.executor._guard_pending_stop(ticket, 1)
    assert ticket.stop_order_id == replacement
    (alarm,) = sent
    assert alarm.endswith("was placed")


def test_unit_unknown_send_outcome_alarms_once_and_says_unknown(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    replacement = f"{ticket.position_id}-stop-wait"
    _ended(stack, monkeypatch, stop_id)
    real_place = stack.broker.place

    def lost(order: Order) -> OrderStatus:
        if order.order_id == replacement:
            raise TimeoutError("no answer")
        return real_place(order)

    monkeypatch.setattr(stack.broker, "place", lost)
    with pytest.raises(TimeoutError):
        stack.executor._guard_pending_stop(ticket, 1)
    assert ticket.stop_order_id == replacement  # G6-2 / R1 unchanged
    (alarm,) = sent
    assert stop_id in alarm
    assert replacement in alarm
    assert "outcome is unknown" in alarm
    assert "TimeoutError" in alarm
    assert "was placed" not in alarm


@pytest.mark.parametrize("error", [RuntimeError, OSError, ValueError])
def test_unit_a_broken_alarm_line_never_stops_the_replacement(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(message: str) -> None:
        raise error("pager down")

    stack.executor.route_alarms(broken)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id)
    stack.executor._guard_pending_stop(ticket, 1)
    replacement = f"{ticket.position_id}-stop-wait"
    assert ticket.stop_order_id == replacement
    assert stack.executor._broker.status(replacement).protects


def test_unit_unrouted_executor_still_replaces_and_logs_the_alarm_critical(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No alarm line routed (as before the runner starts): the replacement still goes,
    and the alarm's text is logged critical."""
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id)
    with caplog.at_level(logging.ERROR, logger="footprint.execution"):
        stack.executor._guard_pending_stop(ticket, 1)
    assert stack.executor._broker.status(f"{ticket.position_id}-stop-wait").protects
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any(WAIT in m and m.endswith("was placed") for m in logged)
    # No line routed is not a failed delivery: nothing is called, nothing fails.
    assert not any("could not deliver" in r.getMessage() for r in caplog.records)


def test_unit_a_stop_reported_filled_gets_no_alarm_no_halt_no_replacement(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that sold the whole position during the wait is a sale, not a gone stop:
    no replacement, no D41 alarm, and not the G6-1 partly-sold halt either."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    _ended(stack, monkeypatch, stop_id, state=OrderState.FILLED, filled=1, quantity=1)
    for _ in range(3):
        stack.executor._guard_pending_stop(ticket, 1)
    assert sent == []
    assert not stack.halt.tripped
    assert ticket.stop_order_id == stop_id
    assert f"{ticket.position_id}-stop-wait" not in stack.broker._orders


# --- review round 1 (PR #53) -------------------------------------------------------------


def test_two_positions_on_one_executor_each_alarm_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: once per POSITION, not once per process: a second position that loses its
    stop during its price wait is alarmed too."""
    sent = _routed(stack)
    t1, stop1 = _ticket(stack)
    t2 = replace(t1, position_id=t1.position_id + "b")
    _ended(stack, monkeypatch, stop1)
    for _ in range(3):
        stack.executor._guard_pending_stop(t1, 1)
        stack.executor._guard_pending_stop(t2, 1)
    first, second = sent
    assert first.startswith(f"{t1.position_id}: ")
    assert first.endswith(f"a new stop {t1.position_id}-stop-wait was placed")
    assert second.startswith(f"{t2.position_id}: ")
    assert second.endswith(f"a new stop {t2.position_id}-stop-wait was placed")


def _place_reports(stack: Stack, monkeypatch: pytest.MonkeyPatch, **changes: object) -> list[str]:
    """The `-stop-wait` send reaches the broker, and the broker's answer to it is
    changed by `changes` (its later status questions are answered the same way)."""
    placing = stack.broker.place
    real = stack.broker.status
    placed: list[str] = []

    def place(order: Order) -> OrderStatus:
        status = placing(order)
        if order.order_id.endswith("-stop-wait"):
            placed.append(order.order_id)
            return replace(status, **changes)  # type: ignore[arg-type]
        return status

    def status(asked: str) -> OrderStatus:
        answer = real(asked)
        if asked in placed:
            return replace(answer, **changes)  # type: ignore[arg-type]
        return answer

    monkeypatch.setattr(stack.broker, "place", place)
    monkeypatch.setattr(stack.broker, "status", status)
    return placed


def test_a_replacement_that_fills_on_placement_says_the_position_is_sold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2 (safety): the market is already through the trigger and the replacement
    fills at once. "not protecting" here could send Andre to place a stop by hand on a
    contract already sold: a short. The alarm says it filled and the position is sold."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    trigger = stack.broker.status(stop_id).order.stop_price
    assert trigger is not None
    stack.quote(trigger - D(2), trigger - D(1), SEND + SEC)  # through the trigger
    stack.executor._guard_pending_stop(ticket, 1)
    replacement = f"{ticket.position_id}-stop-wait"
    assert stack.broker.status(replacement).state is OrderState.FILLED
    (alarm,) = sent
    assert alarm.endswith(
        f"a new stop {replacement} was placed and has already filled (sold 1): the "
        "position is sold; do not place a stop by hand"
    )
    assert "not protecting" not in alarm
    for _ in range(3):
        stack.executor._guard_pending_stop(ticket, 1)
    assert len(sent) == 1
    assert not stack.halt.tripped


def test_a_held_back_replacement_is_not_reported_placed(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (R7): live but held back (IBKR Inactive) is not protection, and it can still
    sell: the alarm says so and warns against a second stop beside it."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    _place_reports(stack, monkeypatch, held_back=True)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    replacement = f"{ticket.position_id}-stop-wait"
    assert alarm.endswith(
        f"a new stop {replacement} was sent, but it is live and held back at the broker: "
        "not protecting, and it can still sell; do not place another stop beside it"
    )
    stack.executor._guard_pending_stop(ticket, 1)
    assert len(sent) == 1
    assert any("held back" in r for r in stack.halt.reasons)  # the existing halt follows


def test_a_replacement_being_cancelled_is_not_reported_placed(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    _place_reports(stack, monkeypatch, cancelling=True)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert alarm.endswith(
        "was sent, but it is live and being cancelled at the broker: not protecting, "
        "and it can still sell; do not place another stop beside it"
    )


def test_a_replacement_that_sold_part_and_ended_says_so(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never "no stop rests" alone for a replacement that already sold: the position
    is (partly) sold, so a hand stop for the whole fill could go short."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    _place_reports(stack, monkeypatch, state=OrderState.CANCELLED, filled=1)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert alarm.endswith(
        "was sent, but it sold 1 and is CANCELLED at the broker: do not place a stop by "
        "hand; reconcile, and check the account"
    )
    stack.executor._guard_pending_stop(ticket, 1)
    assert any("sold 1 of 1" in r for r in stack.halt.reasons)  # G6-1 halt follows


def test_unit_unknown_outcome_names_a_rejected_stop_as_rejected(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (R4): the unknown-outcome alarm names the gone stop's real state."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    replacement = f"{ticket.position_id}-stop-wait"
    _ended(stack, monkeypatch, stop_id, state=OrderState.REJECTED)
    real_place = stack.broker.place

    def lost(order: Order) -> OrderStatus:
        if order.order_id == replacement:
            raise TimeoutError("no answer")
        return real_place(order)

    monkeypatch.setattr(stack.broker, "place", lost)
    with pytest.raises(TimeoutError):
        stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert alarm.startswith(
        f"{ticket.position_id}: stop {stop_id} was rejected at the broker during the price wait"
    )
    assert alarm.endswith(
        "its outcome is unknown: TimeoutError('no answer'); it may be live or filled: "
        "check IBKR before placing any stop by hand"
    )


def test_the_alarm_carries_the_step_time(scene: Scene) -> None:
    """F4: the runner stamps the alarm with the step that sent it, not the start."""
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    _broker_cancels_the_stop(scene)
    scene.step()
    (alarm,) = [a for a in scene.alarms if WAIT in a.message]
    assert alarm.ts == scene.t


# --- review round 2 (PR #53) -------------------------------------------------------------


def test_a_rejected_replacement_that_sold_part_gets_the_partly_sold_wording(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N5: the IBKR adapter reports REJECTED for a replacement whose copies ended in mixed
    terminal states, one of them partly filled. That is a sale of part, never "the
    position is sold": reconcile decides what is held."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    _place_reports(stack, monkeypatch, state=OrderState.REJECTED, filled=1)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert alarm.endswith(
        "was sent, but it sold 1 and is REJECTED at the broker: do not place a stop by "
        "hand; reconcile, and check the account"
    )
    assert "the position is sold" not in alarm


def test_a_live_held_back_replacement_that_sold_part_is_still_worded_live(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2 (unreachable at max_contracts 1; pinned for scaling): a replacement still live
    (held back) that has sold part can still sell more, so the alarm keeps the "live ...
    do not place another stop beside it" wording, not the ended wording."""
    sent = _routed(stack)
    ticket, stop_id = _ticket(stack)
    SimulatedBroker.cancel(stack.broker, stop_id)
    _place_reports(stack, monkeypatch, held_back=True, filled=1)
    stack.executor._guard_pending_stop(ticket, 1)
    (alarm,) = sent
    assert alarm.endswith(
        "was sent, but it is live and held back at the broker: not protecting, and it can "
        "still sell; do not place another stop beside it"
    )
