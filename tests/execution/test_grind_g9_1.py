"""Grind review of 7d5bd77..1241004 (PR #21, claude/grind-review-1241004.md), G9-1 LOW.

G7-3 made a stop check that raises past `fill_price_wait_seconds` still end in
FillUnpriced (CHECK THE FILL and the halt), with BrokerDown alone passed through
unchanged. Its tests pinned that only for TimeoutError, so two reviewer mutants
survived:
- R1: `except Exception` narrowed to `except (RuntimeError, OSError)`: NeverPlaced (a
  KeyError) on the attached stop past the wait escapes every step, and CHECK THE FILL
  never comes for that error;
- R2: `except BrokerDown: raise` widened to `except ConnectionError: raise`: a plain
  ConnectionError that is not BrokerDown (ConnectionResetError) passes unchanged past
  the wait and hides CHECK THE FILL.
These tests pin both, at the unit level and at the runner level (the reviewer's probe
P1). Test only: the code was already right."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.executor.diego import EntryOutcome, EntryTicket
from footprint.execution.orders import (
    FillUnpriced,
    NeverPlaced,
    OrderStatus,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_reconcile_unpriced_entry import Scene, _past_everything

SEC = timedelta(seconds=1)
Make = Callable[[str], Exception]


def _never_placed(order_id: str) -> Exception:
    return NeverPlaced(order_id)


def _reset(order_id: str) -> Exception:
    return ConnectionResetError(f"connection reset asking about {order_id}")


def _ibkr_error(order_id: str) -> Exception:
    return RuntimeError(f"IBKR error [999] for {order_id}")


# Each is an error that is NOT BrokerDown: past the wait it must end in FillUnpriced.
ERRORS = [
    pytest.param(_never_placed, id="NeverPlaced"),
    pytest.param(_reset, id="ConnectionResetError"),
    pytest.param(_ibkr_error, id="RuntimeError"),
]


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _wait(stack: Stack) -> timedelta:
    return timedelta(seconds=stack.profiles.execution.reconcile.fill_price_wait_seconds)


def _pending(stack: Stack) -> tuple[EntryTicket, datetime]:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.WORKING
    since = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    ticket.price_pending_since = since
    return ticket, since


def _stop_status_raises(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    stop_id: str,
    make: Make,
) -> list[str]:
    """Every status of the attached stop, asked through the tracked broker, raises."""
    tracked = stack.executor._broker
    real = tracked.status
    asked: list[str] = []

    def failing(order_id: str) -> OrderStatus:
        if order_id == stop_id:
            asked.append(order_id)
            raise make(order_id)
        return real(order_id)

    monkeypatch.setattr(tracked, "status", failing)
    return asked


# --- unit level: Executor._price_pending, through the real stop check -----------------------


@pytest.mark.parametrize("make", ERRORS)
def test_past_the_wait_a_failing_stop_status_raises_fill_unpriced(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, make: Make
) -> None:
    ticket, since = _pending(stack)
    assert ticket.stop_order_id is not None
    asked = _stop_status_raises(stack, monkeypatch, ticket.stop_order_id, make)
    with pytest.raises(FillUnpriced) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack))
    assert asked == [ticket.stop_order_id]  # the stop was still checked first
    cause = raised.value.__cause__
    assert cause is not None
    assert type(cause) is type(make(ticket.stop_order_id))
    assert not stack.halt.tripped  # the runner halts on FillUnpriced, not the check


@pytest.mark.parametrize("make", ERRORS)
def test_before_the_wait_a_failing_stop_status_raises_its_own_error(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, make: Make
) -> None:
    ticket, since = _pending(stack)
    assert ticket.stop_order_id is not None
    _stop_status_raises(stack, monkeypatch, ticket.stop_order_id, make)
    expected = type(make(ticket.stop_order_id))
    with pytest.raises(expected) as raised:
        stack.executor._price_pending(ticket, 1, since + _wait(stack) - SEC)
    assert type(raised.value) is expected  # unchanged: not FillUnpriced, not wrapped
    assert raised.value.__cause__ is None


# --- runner level: the reviewer's probe P1 ---------------------------------------------------


@pytest.mark.parametrize("make", ERRORS)
def test_a_stop_check_that_keeps_failing_still_ends_in_check_the_fill(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, make: Make
) -> None:
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    asked = _stop_status_raises(scene.stack, monkeypatch, scene.stop, make)
    scene.step()
    assert asked  # the stop is checked while the price is pending
    assert not [m for m in scene.messages() if "CHECK THE FILL" in m]  # not before the wait
    scene.step(_past_everything(scene.stack))
    alarms = [m for m in scene.messages() if "CHECK THE FILL" in m]
    assert len(alarms) == 1  # once
    assert "flatten by hand" in alarms[0]
    assert scene.stack.halt.tripped
    assert scene.lag.sells() == []  # nothing sold on a guess
    assert scene.stop not in scene.lag.cancelled  # its stop is never cancelled
    assert scene.runner.position is None  # nothing booked without a price
    assert SimulatedBroker.positions(scene.stack.broker) == {scene.stack.contract: 1}
