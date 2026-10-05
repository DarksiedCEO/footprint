"""The whole system, through the IBKR adapter, against the fake IBKR."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.core.models import Bar
from footprint.execution.management.malik import ExitReason
from footprint.execution.orders import OrderState, TrackedBroker
from tests.brokers.fake_ibkr import FakeIbkr
from tests.brokers.test_ibkr_adapter import contract
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)
FAST = timedelta(milliseconds=100)


def through_ibkr(stack: Stack, alarms: list[str]) -> tuple[FakeIbkr, IbkrBroker]:
    contracts = ContractBook()
    contracts.register(stack.contract, contract(501))
    fake = FakeIbkr(stack.book, contracts)
    adapter = IbkrBroker(fake, contracts, IbkrSettings(FAST, FAST), ("NQ", "FOP"), alarms.append)
    fake.attach(adapter)
    fake.connect(next_valid_id=1)
    tracked = TrackedBroker(adapter)
    stack.executor._broker = tracked
    stack.manager._broker = tracked
    return fake, adapter


def test_one_trade_through_ibkr_from_decision_to_journal(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter = through_ibkr(stack, [])
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    assert runner.position is not None
    pid = runner.position.position_id
    stop = next(r for r in fake.sends if r.order_ref == f"{pid}-stop")
    assert (stop.order_type, stop.oca_group, stop.oca_type) == ("STP LMT", pid, 1)
    assert adapter.positions() == {stack.contract: 1}
    drop = fill + 30 * SEC
    stack.quote(114, 116, drop)  # the resting stop triggers and fills at 114
    runner.step(drop)
    assert runner.position is None
    assert [c.reason for c in runner.closed] == [ExitReason.STOP]
    (trade,) = journal.trades()
    assert D(trade["net_pnl"]) == (D(114) - D("151.5")) * 20 - 5
    runner.step(drop + SEC)  # reconcile after the close: flat, agreed
    assert not runner.reconciling
    assert adapter.positions() == {}


def test_losing_ibkr_while_holding_halts_and_the_stop_still_protects(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    ib_alarms: list[str] = []
    fake, adapter = through_ibkr(stack, ib_alarms)
    runner, _, _ = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt  # live wiring: a resync sends the runner to reconcile
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    position = runner.position
    assert position is not None
    fake.lose()
    runner.step(fill + SEC)  # management cannot reach IBKR: halt, reconcile
    assert stack.halt.tripped
    assert runner.reconciling
    stop_copy = next(c for c in fake.copies.values() if c.request.order_ref.endswith("-stop"))
    assert fake.sim.status(stop_copy.sim_id).state is OrderState.WORKING  # still resting
    fake.restore(data_lost=True)  # resync, then reconcile
    runner.step(fill + 2 * SEC)
    assert not runner.reconciling  # the broker, the ledger and the records agree
    assert runner.position is position
    assert any("connection lost" in a for a in ib_alarms)
