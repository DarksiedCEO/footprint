"""Grind review of 8ea3ad1..cc98ec0 (PR #21, claude/grind-review-cc98ec0.md), G7-3 LOW.

While an entry the broker reports FILLED waits for its price, its attached stop is
checked every step (Executor._guard_pending_stop, N3-5). That check talks to the
broker and can raise. Before this fix it ran BEFORE the `fill_price_wait_seconds`
test, so a guard that kept raising hid the price wait for ever: CHECK THE FILL (cancel
the entry's stop at IBKR first, then flatten by hand) and its halt never came, and the
contract stayed held (reviewer probe P3).

Now:
- past the wait, a guard that raises still ends in FillUnpriced (so the runner halts
  and alarms CHECK THE FILL once), chained to the guard's own error;
- before the wait, the guard's error is raised unchanged, as before;
- BrokerDown is raised unchanged even past the wait: the runner waits for the broker
  (not a fault), and the next step that reaches it decides CHECK THE FILL;
- the ticket's own replacement stop (`-stop-wait`) that the broker says it never
  received (NeverPlaced, after a send whose outcome was unknown) is a stop that does
  not exist: nothing more is sent, halt "no resting stop ... protect the position by
  hand", as for a refused replacement. NeverPlaced for any other stop is raised as
  before.
Nothing here sends anything new."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.executor.diego import EntryOutcome, EntryTicket
from footprint.execution.orders import (
    BrokerDown,
    FillUnpriced,
    NeverPlaced,
    Order,
    OrderStatus,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_pending_entry_stop import _broker_cancels_the_stop
from tests.execution.test_reconcile_unpriced_entry import Scene, _past_everything

D = Decimal
SEC = timedelta(seconds=1)
_WAIT_SUFFIX = "-stop-wait"


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _wait(stack: Stack) -> timedelta:
    return timedelta(seconds=stack.profiles.execution.reconcile.fill_price_wait_seconds)


def _check_the_fill(scene: Scene) -> list[str]:
    return [m for m in scene.messages() if "CHECK THE FILL" in m]


def _lost_sends(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every send of the replacement stop times out: its outcome is unknown, and the
    simulated broker never received it. The tracked broker tries each send twice under
    the same id, records it as possibly working, and drops that record on the first
    status that says NeverPlaced; from then on NeverPlaced reaches the executor."""
    placing = scene.stack.broker.place
    tried: list[str] = []

    def lost(order: Order) -> OrderStatus:
        if order.order_id.endswith(_WAIT_SUFFIX):
            tried.append(order.order_id)
            raise TimeoutError("no answer")
        return placing(order)

    monkeypatch.setattr(scene.stack.broker, "place", lost)
    return tried


# --- runner level: the reviewer's probe P3 -----------------------------------------------


def test_a_lost_replacement_send_still_ends_in_check_the_fill(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    tried = _lost_sends(scene, monkeypatch)
    _broker_cancels_the_stop(scene)
    scene.step()
    sends = list(tried)
    assert sends  # one id, one send (tried twice inside)
    assert set(sends) == {sends[0]}
    scene.step(_past_everything(scene.stack))
    assert tried == sends  # never sent again, never under a new id
    alarms = _check_the_fill(scene)
    assert len(alarms) == 1  # once
    assert "flatten by hand" in alarms[0]
    assert scene.stack.halt.tripped
    assert scene.lag.sells() == []  # nothing sold on a guess
    assert scene.runner.position is None  # nothing booked without a price
    assert SimulatedBroker.positions(scene.stack.broker) == {scene.stack.contract: 1}


def test_a_replacement_the_broker_never_received_says_protect_by_hand(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NeverPlaced on the ticket's own `-stop-wait` is a stop that does not exist:
    the same halt as a refused replacement, not a failed step every step."""
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    tried = _lost_sends(scene, monkeypatch)
    _broker_cancels_the_stop(scene)
    scene.step()
    sends = list(tried)
    assert sends
    assert set(sends) == {sends[0]}
    scene.step(3)
    assert tried == sends
    reasons = " | ".join(scene.stack.halt.reasons)
    assert "no resting stop" in reasons
    assert "by hand" in reasons
    assert sends[0] in reasons
    assert not any("NeverPlaced" in m for m in scene.messages())
    assert scene.lag.sells() == []
    assert SimulatedBroker.positions(scene.stack.broker) == {scene.stack.contract: 1}


# --- unit level: Executor._price_pending ---------------------------------------------------


def _pending(stack: Stack) -> tuple[EntryTicket, datetime]:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.WORKING
    since = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    ticket.price_pending_since = since
    return ticket, since


def _guard_raises(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> list[EntryTicket]:
    calls: list[EntryTicket] = []

    def failing(ticket: EntryTicket, filled: int) -> None:
        calls.append(ticket)
        raise error

    monkeypatch.setattr(stack.executor, "_guard_pending_stop", failing)
    return calls


def test_past_the_wait_a_raising_guard_still_raises_fill_unpriced(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, since = _pending(stack)
    error = TimeoutError("no answer")
    calls = _guard_raises(stack, monkeypatch, error)
    with pytest.raises(FillUnpriced) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack))
    assert calls == [ticket]  # the guard still ran first
    assert raised.value.__cause__ is error  # the guard's own failure is kept
    assert ticket.order_id in str(raised.value)
    assert ticket.price_pending_since == since  # the clock is not restarted


def test_before_the_wait_a_raising_guard_raises_its_own_error(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, since = _pending(stack)
    error = TimeoutError("no answer")
    _guard_raises(stack, monkeypatch, error)
    with pytest.raises(TimeoutError) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack) - SEC)
    assert raised.value is error


def test_past_the_wait_broker_down_is_raised_unchanged(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner treats BrokerDown as "wait for the broker", not as a fault; the
    first step that reaches the broker again decides CHECK THE FILL."""
    ticket, since = _pending(stack)
    error = BrokerDown("away")
    _guard_raises(stack, monkeypatch, error)
    with pytest.raises(BrokerDown) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack) + SEC)
    assert raised.value is error


def test_past_the_wait_a_quiet_guard_still_raises_fill_unpriced(stack: Stack) -> None:
    """Control: the path that worked before still works."""
    ticket, since = _pending(stack)
    with pytest.raises(FillUnpriced) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack))
    assert raised.value.__cause__ is None


# --- unit level: Executor._guard_pending_stop and NeverPlaced ------------------------------


def test_never_placed_on_the_attached_stop_is_raised_as_before(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the ticket's own replacement is known to be the system's unsent order.
    The attached stop went out with the entry: NeverPlaced there is a fault."""
    ticket, _ = _pending(stack)
    assert ticket.stop_order_id is not None
    tracked = stack.executor._broker
    real = tracked.status

    def unknown(asked: str) -> OrderStatus:
        if asked == ticket.stop_order_id:
            raise NeverPlaced(asked)
        return real(asked)

    monkeypatch.setattr(tracked, "status", unknown)
    with pytest.raises(NeverPlaced):
        stack.executor._guard_pending_stop(ticket, 1)
    assert not stack.halt.tripped


def test_never_placed_on_the_replacement_halts_and_sends_nothing(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, _ = _pending(stack)
    replacement = f"{ticket.position_id}{_WAIT_SUFFIX}"
    ticket.stop_order_id = replacement  # recorded before a send whose outcome is unknown
    sent: list[str] = []
    placing = stack.broker.place

    def recording(order: Order) -> OrderStatus:
        sent.append(order.order_id)
        return placing(order)

    monkeypatch.setattr(stack.broker, "place", recording)
    stack.executor._guard_pending_stop(ticket, 1)
    stack.executor._guard_pending_stop(ticket, 1)
    assert sent == []
    assert ticket.stop_order_id == replacement  # never retried under a new id here
    assert stack.halt.tripped
    reasons = " | ".join(stack.halt.reasons)
    assert "no resting stop" in reasons
    assert "by hand" in reasons
    assert replacement in reasons
