"""A restart never decides a minute twice: the newest bar decided on is durable."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from footprint.core.models import Bar
from footprint.execution.ledger import Ledger
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

MINUTE = timedelta(minutes=1)
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def reopened(stack: Stack) -> Ledger:
    """The ledger as a new process would open it, from the same file."""
    return Ledger(stack.profiles.risk, stack.profiles.instrument, stack.ledger._store)


def test_the_newest_decided_bar_survives_a_restart(stack: Stack) -> None:
    assert stack.ledger.last_decided is None
    stack.ledger.mark_decided(T)
    assert reopened(stack).last_decided == T


def test_it_never_moves_backwards(stack: Stack) -> None:
    stack.ledger.mark_decided(T)
    stack.ledger.mark_decided(T - MINUTE)  # an older bar (a late or replayed feed)
    assert stack.ledger.last_decided == T
    assert reopened(stack).last_decided == T


def test_a_naive_time_is_refused(stack: Stack) -> None:
    with pytest.raises(ValueError, match="UTC"):
        stack.ledger.mark_decided(datetime(2026, 9, 15, 14, 0))  # noqa: DTZ001 (the point)


def test_a_state_file_from_before_this_field_still_opens(stack: Stack, tmp_path: Path) -> None:
    path = stack.ledger._store.path
    raw = json.loads(path.read_text())
    raw.pop("last_decided", None)
    path.write_text(json.dumps(raw))
    assert reopened(stack).last_decided is None


def start(runner: Any) -> None:
    """The first step reconciles; the next decides on the newest completed bar."""
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + timedelta(seconds=1))


def count_decisions(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    seen: list[datetime] = []
    real = stack.pipeline.decide

    def counting(*args: Any) -> Any:
        seen.append(args[-1])
        return real(*args)

    monkeypatch.setattr(stack.pipeline, "decide", counting)
    return seen


def test_a_restarted_runner_does_not_decide_the_same_minute_again(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decided = count_decisions(stack, monkeypatch)
    runner, _, _ = runner_for(stack, bars, tmp_path / "first")
    start(runner)
    assert len(decided) == 1
    newest = runner.last_decided
    assert newest is not None
    assert stack.ledger.last_decided == newest  # saved before anything is acted on
    # The process dies and starts again within the same minute; Andre clears it.
    stack.ledger = reopened(stack)
    restarted, _, _ = runner_for(stack, bars, tmp_path / "second")
    assert restarted.last_decided == newest
    for second in range(20, 40):  # reconcile, then steps through the rest of the minute
        restarted.step(DECISION_TIME + timedelta(seconds=second))
    assert len(decided) == 1  # that minute is not decided again
    bars.append(replace(bars[-1], ts=bars[-1].ts + MINUTE))  # a new minute's bar closes
    restarted.step(DECISION_TIME + MINUTE)
    assert len(decided) == 2  # and that one is decided


def test_the_decided_minute_is_saved_before_the_decision_is_made(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_at_decision: list[datetime | None] = []
    real = stack.pipeline.decide

    def watching(*args: Any) -> Any:
        saved_at_decision.append(reopened(stack).last_decided)
        return real(*args)

    monkeypatch.setattr(stack.pipeline, "decide", watching)
    runner, _, _ = runner_for(stack, bars, tmp_path)
    start(runner)
    assert saved_at_decision == [runner.last_decided]  # durable first: a crash inside the
    # decision (or the send it leads to) can never make a restart decide that minute again


def test_a_minute_that_cannot_be_saved_is_not_decided(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decided = count_decisions(stack, monkeypatch)

    def disk_full(*args: Any) -> None:
        raise OSError("disk full")

    runner, _, alarms = runner_for(stack, bars, tmp_path)
    monkeypatch.setattr(stack.ledger._store, "save", disk_full)
    start(runner)
    assert decided == []  # never decide what a restart could decide again
    assert stack.halt.tripped
    assert any("disk full" in a.message for a in alarms)


# --- review follow-ups ---------------------------------------------------------------------


def test_a_decided_minute_in_the_future_is_alarmed_at_start(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A clock that ran ahead and was corrected would otherwise silence every decision."""
    stack.ledger.mark_decided(DECISION_TIME + timedelta(days=2))
    stack.ledger = reopened(stack)
    _, _, alarms = runner_for(stack, bars, tmp_path)
    assert any("in the future" in a.message for a in alarms)


def test_a_decided_minute_in_the_past_raises_no_alarm(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    stack.ledger.mark_decided(DECISION_TIME - MINUTE)
    stack.ledger = reopened(stack)
    _, _, alarms = runner_for(stack, bars, tmp_path)
    assert not any("in the future" in a.message for a in alarms)


def test_flattening_a_holding_the_ledger_never_recorded_asks_for_it_to_be_booked(
    stack: Stack,
) -> None:
    """The runner died after the entry filled but before the ledger recorded it: the
    restart flattens it, and its P&L must be booked by hand (limits would be wrong)."""
    stack.broker._positions[stack.contract] = 1  # held at the broker; unknown to the ledger
    assert stack.ledger.open_position_id is None
    settle = stack.profiles.execution.reconcile.settle_seconds
    t = DECISION_TIME
    for second in range(2 * settle + 2):
        stack.quote(160, 161, t + second * timedelta(seconds=1))
        stack.manager.reconcile(None, t + second * timedelta(seconds=1))
    booking = [a.message for a in stack.alarms if "book_by_hand" in a.message]
    # Once each, not every round: the unrecorded holding, and (grind review G12-1) the
    # id to book it under, told when the first reconcile order for it is sent.
    unrecorded = [m for m in booking if m.startswith("flattening")]
    assert len(unrecorded) == 1
    assert stack.contract in unrecorded[0]
    named = [m for m in booking if m.startswith("reconcile-")]
    assert len(named) == 1
    assert len(booking) == 2


def test_the_unbooked_holding_is_alarmed_once_even_when_the_flatten_is_sent_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack.broker._positions[stack.contract] = 1
    real_try, real_place = stack.broker._try_fill, stack.broker.place

    def never_fills(status: Any, quote: Any, *, at_touch: bool) -> Any:
        if status.order.order_id.startswith("reconcile-"):
            return status
        return real_try(status, quote, at_touch=at_touch)

    def place_then_lost(order: Any) -> Any:
        status = real_place(order)
        if order.order_id.startswith("reconcile-"):
            stack.broker.cancel(order.order_id)  # the broker drops it: sent again next round
        return status

    monkeypatch.setattr(stack.broker, "_try_fill", never_fills)
    monkeypatch.setattr(stack.broker, "place", place_then_lost)
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(4 * settle + 4):
        t = DECISION_TIME + second * timedelta(seconds=1)
        stack.quote(160, 161, t)
        stack.manager.reconcile(None, t)
    sent = [o for o in stack.broker._orders if o.startswith("reconcile-")]
    assert len(sent) >= 2  # the flatten really was sent more than once
    # Once each, though the flatten is sent again: the unrecorded holding, and the id
    # of the first reconcile order to book it under (grind review G12-1).
    assert sum(a.message.startswith("flattening") for a in stack.alarms) == 1
    assert sum(a.message.startswith(f"{sent[0]} (reconcile)") for a in stack.alarms) == 1
    assert sum("book_by_hand" in a.message for a in stack.alarms) == 2


def test_flattening_a_holding_the_ledger_did_record_is_not_called_unrecorded(
    stack: Stack,
) -> None:
    from tests.execution.test_execution import filled_position

    filled_position(stack)  # recorded in the ledger at its fill
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(2 * settle + 2):
        t = DECISION_TIME + timedelta(minutes=5) + second * timedelta(seconds=1)
        stack.quote(160, 161, t)
        stack.manager.reconcile(None, t)  # a restart: flattened, but it was recorded
    assert not any("never recorded" in a.message for a in stack.alarms)


def test_an_unbooked_holding_already_covered_by_a_stuck_order_is_still_alarmed(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from footprint.execution.orders import Order, OrderType, Side

    stack.broker._positions[stack.contract] = 1  # unknown to the ledger
    stuck = Order("stuck-sell", stack.contract, Side.SELL, 1, OrderType.LIMIT, Decimal(999))
    stack.broker.place(stuck)  # rests far above the market and will not cancel
    real_cancel = stack.broker.cancel
    monkeypatch.setattr(
        stack.broker,
        "cancel",
        lambda order_id: (
            stack.broker.status(order_id) if order_id == "stuck-sell" else real_cancel(order_id)
        ),
    )
    monkeypatch.setattr(stack.broker, "modify", lambda *args: stack.broker.status(args[0]))
    settle = stack.profiles.execution.reconcile.settle_seconds
    for second in range(2 * settle + 2):
        t = DECISION_TIME + second * timedelta(seconds=1)
        stack.quote(160, 161, t)
        stack.manager.reconcile(None, t)
    assert not any(o.startswith("reconcile-") for o in stack.broker._orders)  # covered
    assert sum("never recorded" in a.message for a in stack.alarms) == 1
