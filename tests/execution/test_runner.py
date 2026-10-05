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
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME, chain_for
from tests.execution.conftest import Stack

D = Decimal
SEC = timedelta(seconds=1)


def runner_for(
    stack: Stack, bars: list[Bar], tmp_path: Path, *, cleared: bool = True
) -> tuple[Runner, Journal, list[Alarm]]:
    """A started runner; `cleared` = Andre has cleared it to trade since the start."""
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
        book=stack.book,
        settings=stack.profiles.execution.runner,
    )
    if cleared:
        runner.clear_to_trade("Andre")
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
    assert runner.reconciling  # the ledger still shows the old position: stays dark
    runner.step(t + SEC)
    assert runner.reconciling
    messages = " ".join(a.message for a in alarms)
    assert "reconcile mismatch" in messages  # management alarms reach Andre
    assert "STARTED" in messages


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

    bankroll = stack.profiles.risk.bankroll
    stack.ledger._state = replace(stack.ledger._state, equity=bankroll * D("0.8"))
    runner, _, alarms = runner_for(stack, bars, tmp_path)
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
    assert journal.decisions()[0]["fired"] is True  # the setup was there ...
    assert stack.broker.open_orders() == []  # ... but nothing is sent before the clear
    assert stack.broker.log == []
    with pytest.raises(ValueError, match="name"):
        runner.clear_to_trade("  ")  # a clear needs a name


def test_a_restart_never_resets_the_drawdown_peak(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Review N2: starting and clearing to trade leave the ledger's peak alone."""
    from dataclasses import replace

    bankroll = stack.profiles.risk.bankroll
    stack.ledger._state = replace(stack.ledger._state, equity=bankroll * D("0.9"))
    runner, _, _ = runner_for(stack, bars, tmp_path)  # starts, and Andre clears it
    runner.step(DECISION_TIME)
    account = stack.ledger.account(DECISION_TIME)
    assert account.peak_equity == bankroll
    assert account.equity == bankroll * D("0.9")


def test_a_broken_alarm_channel_never_stops_the_start_up_flatten(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Review N1: an alarm line that throws must not block a safety step."""
    from tests.execution.test_execution import filled_position

    filled_position(stack)
    calls: list[Alarm] = []
    down = True

    def pager(alarm: Alarm) -> None:
        calls.append(alarm)
        if down:
            raise ConnectionError("pager down")

    runner, _, _ = runner_for(stack, bars, tmp_path, cleared=False)
    runner._on_alarm = pager  # the channel breaks after start-up
    t = DECISION_TIME + 60 * SEC
    stack.quote(160, 161, t)
    runner.step(t)  # must not raise
    assert stack.broker.positions() == {}  # flattened anyway
    assert stack.broker.open_orders() == []
    failed = len(calls)
    assert failed == 1  # one try while the line is down, however many alarms
    down = False
    runner.step(t + SEC)  # the channel is back: what failed is delivered
    first_failed = calls[0]
    assert any(a is first_failed for a in calls[failed:])  # the same alarm, retried
    calls.clear()
    runner.step(t + 2 * SEC)
    assert not any(a is first_failed for a in calls)  # delivered once, then dropped


def test_a_ledger_failure_inside_a_step_halts_instead_of_escaping(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review N3: the latch watch runs inside the step's guard."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)

    def broken(now: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(stack.ledger, "account", broken)
    runner.step(DECISION_TIME + SEC)  # must not raise
    assert stack.halt.tripped
    assert runner.reconciling
    assert any("step failed" in a.message for a in alarms)


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


def test_a_quiet_feed_never_fills_a_stepped_entry_on_an_old_quote(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Review N4 at the runner: each step marks the clock, so the last quote of a feed
    that went quiet goes stale and the next price step cannot fill against it."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)  # entry working at the 151 floor
    stack.quote(150, D("151.25"), DECISION_TIME + 2 * SEC)  # then the feed goes quiet
    runner.step(DECISION_TIME + 11 * SEC)  # steps to 151.5, above that old ask
    assert runner.position is None
    assert stack.broker.positions() == {}


# --- review round 3 ---------------------------------------------------------------------


def open_trade(stack: Stack, runner: Runner) -> datetime:
    """Run the runner into a protected position (filled at 151.5); returns the fill time."""
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    assert runner.position is not None
    return fill


def test_a_broken_news_calendar_flattens_and_blocks_entries(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """R3-1: an unreadable calendar is a blackout in force, not a crash loop."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def broken(now: datetime) -> tuple[()]:
        raise ConnectionError("calendar down")

    runner._blackouts = broken
    t = fill + SEC
    stack.quote(150, 152, t)
    runner.step(t)
    assert runner.position is None
    assert [c.reason for c in runner.closed] == [ExitReason.NEWS]
    assert stack.broker.positions() == {}
    assert not stack.halt.tripped  # handled, not a step failure
    runner.step(t + SEC)
    assert sum("calendar unreadable" in a.message for a in alarms) == 1


def test_a_position_that_cannot_be_managed_is_flattened_by_the_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-1: management keeps crashing, reconcile keeps agreeing; the failsafe still
    gets the position out through the broker alone."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def broken(*args: object) -> None:
        raise OSError("stop keeper broken")

    monkeypatch.setattr(stack.stops, "check", broken)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    t = fill
    while (t - fill).total_seconds() < limit:  # up to exactly the limit
        t += SEC
        stack.quote(150, 152, t)
        runner.step(t)
        assert runner.position is not None  # not before the limit
    t += SEC
    stack.quote(150, 152, t)
    runner.step(t)  # the failsafe exit sells at the bid
    assert runner.position is None
    assert stack.broker.positions() == {}
    assert runner.closed[-1].reason is ExitReason.FAILSAFE
    assert stack.ledger.open_position_id is None
    assert sum("unmanaged since" in a.message for a in alarms) == 1
    assert not runner._failsafe_alarmed  # T5: the next position gets its own alarm


def test_the_failsafe_stands_aside_while_reconcile_is_flattening(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    calls: list[object] = []
    monkeypatch.setattr(stack.manager, "flatten", lambda *a: calls.append(a))
    runner._managed_at = fill - timedelta(hours=1)
    runner._reconcile_flattening = True  # reconcile disagreed and is selling
    runner._failsafe(fill + SEC)
    assert calls == []


def test_a_retry_storm_is_impossible_while_the_pager_is_down(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-2: one pager try per step, after the safety work; no duplicate queue."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    tries: list[str] = []

    def pager(alarm: Alarm) -> None:
        tries.append(alarm.message)
        raise ConnectionError("pager down")

    def broken(now: datetime) -> list[Bar]:
        raise OSError("bars feed down")

    runner._on_alarm = pager
    runner._bars = broken
    for second in range(1, 61):
        before = len(tries)
        runner.step(DECISION_TIME + second * SEC)
        assert len(tries) - before <= 1
    assert len(runner._undelivered) <= 3
    messages = [a.message for a in runner._undelivered]
    assert len(messages) == len(set(messages))


def test_a_changing_exception_text_never_grows_the_halt_reasons(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    n = {"i": 0}

    def broken(now: datetime) -> list[Bar]:
        n["i"] += 1
        raise OSError(f"bars feed down at attempt {n['i']}")

    runner._bars = broken
    for second in range(1, 11):
        runner.step(DECISION_TIME + second * SEC)
    assert stack.halt.reasons == ("step failed: OSError",)
    assert sum("step failed" in a.message and "HALT" not in a.message for a in alarms) == 1


def test_a_close_found_by_reconcile_is_journaled(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Round-1 M3, now guarded: a trade reconcile books still reaches the journal."""
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    runner.doubt()
    drop = fill + SEC
    stack.quote(114, 116, drop)  # the resting stop fills while the runner is in doubt
    runner.step(drop)
    assert runner.position is None
    assert [c.reason for c in runner.closed] == [ExitReason.RECONCILE]
    (trade,) = journal.trades()
    assert D(trade["net_pnl"]) == (D(114) - D("151.5")) * 20 - 5  # the real fill


def test_an_immediate_unprotected_fill_is_flattened_on_the_same_step(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Round-1 M1, now guarded: filled at once, stop refused, out in the same step."""

    class FillsAtOnceRefusesStops(SimulatedBroker):
        def place(self, order: Order) -> OrderStatus:
            if order.order_type is OrderType.STOP_LIMIT:
                return OrderStatus(order, OrderState.REJECTED, 0, None)
            status = super().place(order)
            if order.order_id.endswith("-entry"):
                self._positions[order.contract_id] = order.quantity
                status = OrderStatus(order, OrderState.FILLED, order.quantity, order.limit_price)
                self._orders[order.order_id] = status
            return status

    broker = FillsAtOnceRefusesStops(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    stack.executor._broker = broker
    stack.manager._broker = broker
    stack.broker = broker
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    runner.step(DECISION_TIME + SEC)
    assert broker.positions() == {}
    assert runner.position is None
    assert [c.reason for c in runner.closed] == [ExitReason.UNPROTECTED]
    assert any("filled without a resting stop" in a.message for a in alarms)


def test_reconcile_accepts_a_working_exit(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A position mid-exit (stop cancelled, exit working) is a known state."""
    from tests.execution.test_execution import exit_rests_on_arrival

    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    position = runner.position
    assert position is not None
    exit_rests_on_arrival(stack, monkeypatch)
    t = fill + SEC
    stack.quote(150, 152, t)
    assert stack.manager.flatten(position, t) is None  # the exit rests
    assert position.exit_order_id is not None
    assert position.stop_order_id is None
    assert stack.manager.reconcile(position, t + SEC)
    assert not any("reconcile mismatch" in a.message for a in alarms)


def test_a_repeating_alarm_is_queued_once_while_the_pager_is_down(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def pager(alarm: Alarm) -> None:
        raise ConnectionError("pager down")

    runner._on_alarm = pager
    for second in range(10, 20):  # no quotes: "no fresh quote" every step
        runner.step(fill + second * SEC)
    messages = [a.message for a in runner._undelivered]
    assert any("no fresh quote" in m for m in messages)
    assert len(messages) == len(set(messages))


def test_a_failsafe_that_cannot_get_out_alarms_once_and_keeps_trying(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    tries = {"n": 0}

    def blind(order_id: str) -> OrderStatus:
        tries["n"] += 1
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(stack.broker, "status", blind)
    monkeypatch.setattr(stack.broker, "cancel", blind)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, limit + 10):
        runner.step(fill + second * SEC)
    before = tries["n"]
    for second in range(limit + 10, limit + 20):
        runner.step(fill + second * SEC)
    assert runner.position is not None  # cannot get out while the broker is blind ...
    assert sum("unmanaged since" in a.message for a in alarms) == 1  # ... said once
    assert stack.halt.tripped
    assert tries["n"] > before  # ... and it never stops trying


# --- review round 4 ---------------------------------------------------------------------


def test_a_failing_ledger_read_never_disarms_the_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-1: every step goes back to reconcile, which agrees; the failsafe still acts."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def broken(now: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stack.ledger, "account", broken)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, limit + 10):
        t = fill + second * SEC
        stack.quote(150, 152, t)
        runner.step(t)
    assert stack.broker.positions() == {}  # out, at the bid
    assert stack.broker.open_orders() == []


def test_a_healthy_position_is_never_flattened_by_the_failsafe(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """T1: management that keeps succeeding keeps the failsafe away."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, 3 * limit):
        t = fill + second * SEC
        stack.quote(150, 152, t)
        runner.step(t)
    assert runner.position is not None
    assert not any("unmanaged since" in a.message for a in alarms)


def test_the_failsafe_clock_starts_at_the_fill(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2: a position whose very first management fails is still covered."""

    def broken(*args: object) -> None:
        raise OSError("stop keeper broken")

    runner, _, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    monkeypatch.setattr(stack.stops, "check", broken)  # before the first manage
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    limit = stack.profiles.execution.runner.max_unmanaged_seconds
    for second in range(1, limit + 5):
        t = fill + second * SEC
        stack.quote(150, 152, t)
        runner.step(t)
    assert stack.broker.positions() == {}
    assert runner.closed[-1].reason is ExitReason.FAILSAFE


def test_a_failsafe_that_raises_halts_and_doubts(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)

    def broken(*args: object) -> None:
        raise ConnectionError("broker gone")

    monkeypatch.setattr(stack.manager, "flatten", broken)
    runner._managed_at = fill - timedelta(hours=1)
    runner._failsafe(fill + SEC)  # must not raise
    assert runner.reconciling
    assert "failsafe flatten failed: ConnectionError" in stack.halt.reasons


def test_the_failsafe_never_sells_beside_a_reconcile_flatten(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Reconcile disagrees and posts its own sell; an overdue failsafe must not add a
    second one (it would sell the contract twice and leave a short option)."""
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    stray = Order("stray", "OTHER", Side.BUY, 1, OrderType.LIMIT, D(1))
    stack.broker.place(stray)  # an order nobody expects: reconcile will disagree
    runner._managed_at = fill - timedelta(hours=1)  # and the failsafe is overdue
    runner.doubt()
    t = fill + 10 * SEC  # the feed is quiet: reconcile's sell rests
    runner.step(t)
    sells = [o for o in stack.broker.open_orders() if o.side is Side.SELL]
    assert [o.order_id.startswith("reconcile-") for o in sells] == [True]
    stack.quote(150, 152, t + SEC)  # a live bid: everything that rests can fill
    assert all(qty >= 0 for qty in stack.broker.positions().values())
