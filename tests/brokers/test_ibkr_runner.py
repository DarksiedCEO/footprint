"""The whole system, through the IBKR adapter, against the fake IBKR."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.core.models import Bar
from footprint.execution.management.malik import ExitReason
from footprint.execution.orders import OrderState, TrackedBroker
from footprint.execution.runner import Runner
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


def test_losing_ibkr_while_holding_waits_without_a_halt_and_the_stop_still_protects(
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
    runner.step(fill + SEC)  # management cannot reach IBKR: reconcile once it is back
    assert not stack.halt.tripped  # an outage (e.g. Gateway's nightly restart) is no fault
    assert runner.reconciling
    stop_copy = next(c for c in fake.copies.values() if c.request.order_ref.endswith("-stop"))
    assert fake.sim.status(stop_copy.sim_id).state is OrderState.WORKING  # still resting
    fake.restore(data_lost=True)  # resync, then reconcile
    runner.step(fill + 2 * SEC)
    assert not runner.reconciling  # the broker, the ledger and the records agree
    assert runner.position is position
    assert any("connection lost" in a for a in ib_alarms)
    assert not stack.halt.tripped  # entries stay possible after the outage


def open_through_ibkr(
    stack: Stack, bars: list[Bar], tmp_path: Path, alarms: list[str]
) -> tuple[FakeIbkr, IbkrBroker, Runner, datetime]:
    fake, adapter = through_ibkr(stack, alarms)
    runner, _, _ = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    assert runner.position is not None
    return fake, adapter, runner, fill


def test_a_stop_ibkr_makes_inactive_ends_flat_never_unprotected(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    ib_alarms: list[str] = []
    fake, adapter, runner, fill = open_through_ibkr(stack, bars, tmp_path, ib_alarms)
    stop = next(c for c in fake.copies.values() if c.request.order_ref.endswith("-stop"))
    fake.inactivate(stop.perm_id)  # the adapter cancels it at once
    assert any("Inactive" in a for a in ib_alarms)
    t = fill + SEC
    stack.quote(150, 152, t)
    runner.step(t)
    assert runner.position is None  # flattened as unprotected
    assert [c.reason for c in runner.closed] == [ExitReason.UNPROTECTED]
    assert adapter.positions() == {}


def test_a_position_report_a_step_behind_the_close_does_not_halt(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, _, runner, fill = open_through_ibkr(stack, bars, tmp_path, [])
    fake.position_lag = True
    drop = fill + 30 * SEC
    stack.quote(114, 116, drop)  # the stop fills; IBKR's report lags behind
    runner.step(drop)
    assert runner.position is None
    runner.step(drop + SEC)  # reconcile: positions uncertain, so it waits
    assert not stack.halt.tripped
    fake.flush_positions()
    runner.step(drop + 2 * SEC)
    assert not runner.reconciling  # agreed
    assert not stack.halt.tripped  # entries were never shut off


def test_an_entry_whose_fill_is_told_before_its_open_order_is_protected(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter, runner, _ = open_through_ibkr_status_first(stack, bars, tmp_path)
    assert runner.position is not None  # the fill was seen: a position, not "unfilled"
    pid = runner.position.position_id
    assert any(r.order_ref == f"{pid}-stop" for r in fake.sends)  # and it has its stop
    assert adapter.positions() == {stack.contract: 1}


def open_through_ibkr_status_first(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> tuple[FakeIbkr, IbkrBroker, Runner, datetime]:
    fake, adapter = through_ibkr(stack, [])
    fake.status_first = True
    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    return fake, adapter, runner, fill


# --- PR C review: starting before IBKR answers, and the failsafe during an outage ---------


def test_a_runner_started_before_ibkr_connects_waits_then_trades(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The live program steps the runner from its first second, before IB Gateway has
    connected and resynced: that is waiting, never a halt."""
    contracts = ContractBook()
    contracts.register(stack.contract, contract(501))
    fake = FakeIbkr(stack.book, contracts)
    adapter = IbkrBroker(fake, contracts, IbkrSettings(FAST, FAST), ("NQ", "FOP"))
    fake.attach(adapter)
    tracked = TrackedBroker(adapter)
    stack.executor._broker = tracked
    stack.manager._broker = tracked
    runner, _, _ = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt
    for second in range(5):  # IBKR not there yet
        runner.step(DECISION_TIME + second * SEC)
    assert not stack.halt.tripped
    assert runner.reconciling
    fake.connect(next_valid_id=1)  # connects and resyncs
    for second in range(5, 17):
        stack.quote(150, 152, DECISION_TIME + second * SEC)  # prices keep arriving
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 17 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    assert not stack.halt.tripped
    assert runner.position is not None  # an entry went out and filled


def test_the_failsafe_waits_without_a_halt_while_ibkr_is_down(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, _, runner, fill = open_through_ibkr(stack, bars, tmp_path, [])
    fake.lose()
    unmanaged = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, unmanaged + 5):  # long past the failsafe's limit
        runner.step(fill + second * SEC)
    assert not stack.halt.tripped  # nothing could be sent; that is no fault
    assert runner.position is not None  # still the system's, its stop resting at IBKR


def test_a_failsafe_flatten_that_cannot_reach_the_broker_is_no_halt(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    from footprint.brokers.ibkr.adapter import BrokerUnavailable

    _, _, runner, fill = open_through_ibkr(stack, bars, tmp_path, [])

    def unreachable(*args: object) -> None:
        raise BrokerUnavailable("IB Gateway is restarting")

    stack.manager.flatten = unreachable  # type: ignore[assignment]
    stack.manager.manage = unreachable  # type: ignore[assignment]
    unmanaged = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, unmanaged + 5):
        runner.step(fill + second * SEC)
    assert not stack.halt.tripped
    assert runner.reconciling
