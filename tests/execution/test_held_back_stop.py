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
    before = len(alarms)
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for _ in range(6 * escalate):
        stack.quote(150, D(151), t)
        runner.step(t)
        t += SEC
    said = [a.message for a in alarms[before:]]
    held = fake.sim.positions()
    assert any("reconcile mismatch" in m for m in said), said  # never silently "agreed"
    assert stack.halt.tripped
    # Out, or Andre told the stop is stuck: never managed on as if protected.
    assert held == {} or any("CANCEL STUCK" in m for m in said), (held, said)
    assert all(qty >= 0 for qty in held.values())  # never sold beside the stuck stop
    sells = [
        c
        for c in fake.copies.values()
        if c.request.action == "SELL" and fake.sim.status(c.sim_id).state is OrderState.WORKING
    ]
    assert sum(c.request.quantity for c in sells) <= sum(held.values())  # never more than held
