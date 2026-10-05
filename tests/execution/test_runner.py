"""The runner end to end on the synthetic market: start-up reconcile, one decision
per minute, entry, resting stop, management, journal."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.core.options import OptionChain
from footprint.execution.journal.josephine import Journal
from footprint.execution.management.malik import Alarm, ExitReason
from footprint.execution.orders import Order, OrderStatus
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME, chain_for
from tests.execution.conftest import Stack

D = Decimal
SEC = timedelta(seconds=1)


def runner_for(
    stack: Stack, bars: list[Bar], tmp_path: Path, *, cleared: bool = True
) -> tuple[Runner, Journal, list[Alarm]]:
    """A started runner; `cleared` = Andre has cleared the start-up kill switch."""
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
        started=DECISION_TIME - SEC,
    )
    if cleared:
        stack.ledger.clear_kill_switch("Andre")
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
    runner, _, alarms = runner_for(stack, bars, tmp_path, cleared=False)
    t = DECISION_TIME + 60 * SEC
    stack.quote(160, 161, t)
    runner.step(t)
    assert stack.broker.positions() == {}
    assert stack.broker.open_orders() == []
    assert stack.ledger.account(t).kill_switch_latched
    assert runner.reconciling  # the ledger still shows the old position: stays dark
    runner.step(t + SEC)
    assert runner.reconciling
    messages = " ".join(a.message for a in alarms)
    assert "reconcile mismatch" in messages  # management alarms reach Andre
    assert "KILL SWITCH LATCHED" in messages


def test_no_decisions_while_halted_entries_are_refused(
    stack: Stack,
    bars: list[Bar],
    tmp_path: Path,
) -> None:
    runner, journal, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    stack.halt.trip("test")
    assert any(a.message == "HALT: test" for a in alarms)  # halts reach Andre
    runner.step(DECISION_TIME + SEC)
    assert journal.decisions()[0]["fired"] is True  # the setup was there ...
    assert stack.broker.open_orders() == []  # ... but nothing was sent
    runner.step(DECISION_TIME + 20 * SEC)
    assert len(journal.decisions()) == 1  # once per completed minute, not per step


def test_a_forming_bar_never_triggers_a_new_decision(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    live = list(bars)
    runner, journal, _ = runner_for(stack, live, tmp_path)
    runner.step(DECISION_TIME)
    stack.halt.trip("test")  # keep it deciding without trading
    runner.step(DECISION_TIME + SEC)
    last = live[-1]
    live.append(Bar("NQ", DECISION_TIME, last.close, last.close + 1, last.close - 1, last.close, 1))
    runner.step(DECISION_TIME + 30 * SEC)  # the 07:01 bar is still forming
    assert len(journal.decisions()) == 1
    runner.step(DECISION_TIME + 60 * SEC)  # now it has closed
    assert len(journal.decisions()) == 2


def test_a_drawdown_decision_latches_the_kill_switch(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    from dataclasses import replace

    runner, _, alarms = runner_for(stack, bars, tmp_path)
    bankroll = stack.profiles.risk.bankroll
    stack.ledger._state = replace(stack.ledger._state, equity=bankroll * D("0.8"))
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)
    assert stack.ledger.account(DECISION_TIME + SEC).kill_switch_latched
    assert any("KILL SWITCH LATCHED" in a.message for a in alarms)


# --- review findings ------------------------------------------------------------------


def test_a_restart_never_trades_until_andre_clears_it(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, journal, _ = runner_for(stack, bars, tmp_path, cleared=False)
    runner.step(DECISION_TIME)  # flat broker, clean ledger: reconcile agrees
    assert not runner.reconciling
    runner.step(DECISION_TIME + SEC)
    assert journal.decisions()[0]["fired"] is False
    assert "risk: KILL_SWITCH" in journal.decisions()[0]["reasons"]
    assert stack.broker.open_orders() == []


def test_a_broker_error_on_the_entry_reconciles_instead_of_crashing(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    original = stack.broker.place

    def lost_ack(order: Order) -> OrderStatus:
        original(order)  # it reached the broker ...
        raise ConnectionError("... but the answer was lost")

    monkeypatch.setattr(stack.broker, "place", lost_ack)
    runner.step(DECISION_TIME + SEC)  # must not raise
    assert runner.reconciling
    assert stack.halt.tripped
    assert any("step failed" in a.message for a in alarms)
    monkeypatch.setattr(stack.broker, "place", original)
    runner.step(DECISION_TIME + 2 * SEC)  # reconcile cancels the orphan entry
    assert stack.broker.open_orders() == []
    stack.quote(150, D(151), DECISION_TIME + 3 * SEC)  # it can no longer fill
    assert stack.broker.positions() == {}


def test_a_doubt_with_a_working_entry_abandons_it(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)  # entry working at 151
    assert stack.broker.open_orders()
    runner.doubt()
    runner.step(DECISION_TIME + 2 * SEC)
    assert stack.broker.open_orders() == []
    # The runner cancelled its own entry first, so reconcile found nothing amiss.
    assert not runner.reconciling
    assert not any("reconcile mismatch" in a.message for a in alarms)
    stack.quote(150, D(151), DECISION_TIME + 3 * SEC)
    runner.step(DECISION_TIME + 3 * SEC)
    assert stack.broker.positions() == {}  # nothing filled, no stop was ever sent


def test_a_journal_failure_never_skips_the_latch(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    runner, journal, alarms = runner_for(stack, bars, tmp_path)
    bankroll = stack.profiles.risk.bankroll
    stack.ledger._state = replace(stack.ledger._state, equity=bankroll * D("0.8"))

    def broken(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(journal, "record_decision", broken)
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)
    assert stack.ledger.account(DECISION_TIME + SEC).kill_switch_latched
    assert any("journal failed" in a.message for a in alarms)


def test_an_entry_against_a_moved_market_is_refused(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    stack.quote(100, 101, DECISION_TIME + SEC)  # the book disagrees with the chain
    runner.step(DECISION_TIME + SEC)
    assert stack.broker.open_orders() == []
