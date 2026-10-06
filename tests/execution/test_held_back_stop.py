"""AEGIS I-1: a held position whose resting stop goes Inactive at IBKR, and whose cancel
IBKR never answers. Through the runner, over the fake IBKR: the system must never go on
"managing" an unprotected position as if protected: reconcile disagrees and halts, then
gets out (here by re-pricing the stuck stop) or escalates the stuck cancel to Andre
(CANCEL STUCK), and never sells beside the stuck stop."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.core.models import Bar
from footprint.execution.orders import OrderState, TrackedBroker
from tests.brokers.fake_ibkr import FakeIbkr
from tests.brokers.test_ibkr_adapter import FAST, contract
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)


def through_ibkr(stack: Stack) -> tuple[FakeIbkr, IbkrBroker]:
    """The stack's executor and manager over the real adapter and a fake IBKR."""
    contracts = ContractBook()
    contracts.register(stack.contract, contract(501))
    fake = FakeIbkr(stack.book, contracts)
    adapter = IbkrBroker(fake, contracts, IbkrSettings(FAST, FAST, FAST * 3), ("NQ", "FOP"))
    fake.attach(adapter)
    fake.connect(next_valid_id=1)
    tracked = TrackedBroker(adapter)
    stack.executor._broker = tracked
    stack.manager._broker = tracked
    return fake, adapter


def test_a_held_position_with_an_unanswered_inactive_stop_escalates_to_andre(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):  # the entry, then its fill
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    position = runner.position
    assert position is not None
    stop = next(c for c in fake.copies.values() if c.request.order_ref == position.stop_order_id)
    fake.mute_cancels = True  # IBKR never answers a cancel
    fake.inactivate(stop.perm_id)
    before, sent_before = len(alarms), len(fake.sends)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(6 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    held = fake.sim.positions()
    assert any("reconcile mismatch" in m for m in said), said  # never silently "agreed"
    assert stack.halt.tripped
    # The stuck stop is re-priced toward the market (a modify to IBKR), never doubled.
    assert any(r.order_ref == position.stop_order_id for r in fake.sends[sent_before:])
    # Out, or Andre told the stop is stuck: never managed on as if protected.
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())  # never sold beside the stuck stop
    sells = [
        c
        for c in fake.copies.values()
        if c.request.action == "SELL" and fake.sim.status(c.sim_id).state is OrderState.WORKING
    ]
    assert sum(c.request.quantity for c in sells) <= sum(held.values())  # never more than held


def test_a_working_stop_whose_cancel_stays_pending_escalates_to_andre(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS J-1: an ordinary working stop; an exit cancels it and IBKR leaves the
    cancel pending forever. A stop being cancelled is no protection: reconcile must
    disagree and escalate (CANCEL STUCK), never loop or carry the position on."""
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    position = runner.position
    assert position is not None
    fake.slow_cancels = True  # every cancel stays PendingCancel: IBKR never finishes it
    fake.cancel_answer = None
    stack.ledger.latch_kill_switch("test: force an exit")  # management must get out
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(8 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    held = fake.sim.positions()
    assert any("reconcile mismatch" in m for m in said), said
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())


def _held_position(stack: Stack, bars: list[Bar], tmp_path: Path):  # type: ignore[no-untyped-def]
    fake, adapter = through_ibkr(stack)
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    t = DECISION_TIME
    for k in range(120):
        if runner.position is not None:
            break
        stack.quote(150, D(152) if k < 3 else D(151), t)
        runner.step(t)
        t += SEC
    assert runner.position is not None
    return fake, adapter, runner, alarms, t


def test_a_normal_exit_through_a_brief_pending_cancel_and_a_doubt_ends_cleanly(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS K-1/K-3: an ordinary exit whose stop cancel sits in PendingCancel for a few
    seconds (under cancel_escalate_seconds), with a doubt meanwhile, is a healthy exit:
    no reconcile mismatch, no "held back" alarm, closed with its own reason."""
    fake, _, runner, alarms, t = _held_position(stack, bars, tmp_path)
    fake.slow_cancels = True
    stack.ledger.latch_kill_switch("test: force an exit")
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    pending = escalate // 2
    for k in range(pending):
        if k == 1:
            runner.doubt()  # reconcile runs while the cancel is pending
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    assert not stack.halt.tripped
    fake.slow_cancels = False
    fake.finish_cancels()
    for _ in range(2 * escalate):
        if runner.position is None and not runner.reconciling:
            break
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    assert not any("reconcile mismatch" in m for m in said), said
    assert not any("held back" in m for m in said), said
    assert not any("not by this system" in m for m in said), said
    assert runner.position is None
    assert [c.reason.value for c in runner.closed][-1:] == ["KILL_SWITCH"], runner.closed
    assert fake.sim.positions() == {}


def test_a_stop_cancelled_by_someone_else_is_no_protection(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """A stop whose cancel this system never sent (by hand at IBKR), left pending: an
    alarm, then the exit; never managed on as if protected, never a sell beside it."""
    fake, adapter, runner, alarms, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    assert position.stop_order_id is not None
    fake.slow_cancels = True
    adapter.cancel(position.stop_order_id)  # not through management
    assert adapter.status(position.stop_order_id).cancelling
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(6 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    assert sum("not by this system" in m for m in said) == 1, said
    held = fake.sim.positions()
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())


def test_reconcile_agrees_with_a_pending_cancel_only_until_the_escalation(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """AEGIS K-1: reconcile agrees with a stop whose cancel is pending for under
    cancel_escalate_seconds (a normal exit passes through it), and no longer after."""
    fake, adapter, runner, _, t = _held_position(stack, bars, tmp_path)
    position = runner.position
    assert position is not None
    assert position.stop_order_id is not None
    fake.slow_cancels = True
    adapter.cancel(position.stop_order_id)
    escalate = timedelta(seconds=stack.profiles.execution.reconcile.cancel_escalate_seconds)
    manager = stack.manager
    assert manager.reconcile(position, t)
    assert manager.reconcile(position, t + escalate - SEC)
    assert not manager.reconcile(position, t + escalate)
