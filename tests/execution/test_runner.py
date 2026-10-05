"""The runner end to end on the synthetic market: start-up reconcile, one decision
per minute, entry, resting stop, management, journal."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from footprint.core.models import Bar
from footprint.core.options import OptionChain
from footprint.execution.journal.josephine import Journal
from footprint.execution.management.malik import Alarm, ExitReason
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME, chain_for
from tests.execution.conftest import Stack

D = Decimal
SEC = timedelta(seconds=1)


def runner_for(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> tuple[Runner, Journal, list[Alarm]]:
    journal = Journal(tmp_path / "journal")
    alarms: list[Alarm] = []

    def chain(now: datetime) -> OptionChain | None:
        closed = [b for b in bars if b.ts + timedelta(minutes=1) <= now]
        return chain_for(closed[-1].close, now) if closed else None

    runner = Runner(
        stack.pipeline,
        stack.executor,
        stack.manager,
        stack.ledger,
        journal,
        stack.halt,
        bars=lambda now: bars,
        chain=chain,
        blackouts=lambda now: (),
        on_alarm=alarms.append,
    )
    return runner, journal, alarms


def test_one_trade_from_decision_to_journal(stack: Stack, bars: list[Bar], tmp_path: Path) -> None:
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)  # start-up reconcile: everything agrees
    assert not runner.reconciling
    t = DECISION_TIME + SEC
    runner.step(t)  # decides and sends the entry at the 151 floor
    assert len(journal.decisions()) == 1
    assert journal.decisions()[0]["fired"] is True
    for second in range(2, 12):
        runner.step(DECISION_TIME + second * SEC)  # steps toward the ceiling
    assert len(journal.decisions()) == 1  # still the same minute: no second decision
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)  # the ask comes down: filled at 151.5
    runner.step(fill)
    assert runner.position is not None
    assert stack.broker.open_orders()[0].order_type.value == "STOP_LIMIT"
    drop = fill + 30 * SEC
    stack.quote(114, 116, drop)  # the broker's stop fills at 114
    runner.step(drop)
    assert runner.position is None
    assert [c.reason for c in runner.closed] == [ExitReason.STOP]
    (trade,) = journal.trades()
    assert D(trade["net_pnl"]) == (D(114) - D("151.5")) * 20 - 5
    assert stack.ledger.account(drop).stopouts_today == 1


def test_start_up_flattens_whatever_the_broker_still_holds(
    stack: Stack,
    bars: list[Bar],
    tmp_path: Path,
) -> None:
    from tests.execution.test_execution import filled_position

    filled_position(stack)  # a position from before the "restart"
    runner, _, _ = runner_for(stack, bars, tmp_path)
    t = DECISION_TIME + 60 * SEC
    stack.quote(160, 161, t)
    runner.step(t)
    assert stack.broker.positions() == {}
    assert stack.broker.open_orders() == []
    assert stack.ledger.account(t).kill_switch_latched
    assert runner.reconciling  # the ledger still shows the old position: stays dark
    runner.step(t + SEC)
    assert runner.reconciling


def test_no_decisions_while_halted_entries_are_refused(
    stack: Stack,
    bars: list[Bar],
    tmp_path: Path,
) -> None:
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    stack.halt.trip("test")
    runner.step(DECISION_TIME + SEC)
    assert journal.decisions()[0]["fired"] is True  # the setup was there ...
    assert stack.broker.open_orders() == []  # ... but nothing was sent
