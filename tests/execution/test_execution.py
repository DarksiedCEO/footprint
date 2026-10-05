"""Diego (execution), Malik (trade management), the ledger and Josephine (journal),
tested from the spec: G5, G6, G8, G15, news rules, NB-02, NB-03, NB-07, NB-12, NB-16."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.calendar import SessionKind
from footprint.decision.session import NewsBlackout
from footprint.execution.executor.diego import (
    EntryOutcome,
    EntryRefused,
    EntryTicket,
    Position,
)
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import ExitReason
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.decision.scenario import DECISION_TIME, PT, chain_for
from tests.execution.conftest import SEND, Stack

D = Decimal
SEC = timedelta(seconds=1)


FILLED_AT = SEND + 10 * SEC  # the entry has stepped to its 151.5 ceiling by then


def filled_position(stack: Stack) -> Position:
    """Submit, step to the ceiling, then let the ask come down so the entry fills at
    151.5 (the simulator fills at the limit price)."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stack.quote(150, D("151.5"), FILLED_AT)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    assert ticket.outcome is EntryOutcome.PROTECTED, ticket.outcome
    assert ticket.position is not None
    assert ticket.position.entry_price == D("151.5")
    return ticket.position


# --- orders (NB-12) -------------------------------------------------------------


def test_nb12_there_is_no_market_order_type() -> None:
    assert {t.name for t in OrderType} == {"LIMIT", "STOP_LIMIT"}
    assert not hasattr(OrderType, "MARKET")


def test_orders_validate() -> None:
    with pytest.raises(ValueError, match="stop_price"):
        Order("o", "c", Side.SELL, 1, OrderType.STOP_LIMIT, D(10))
    with pytest.raises(ValueError, match="at or below its stop"):
        Order("o", "c", Side.SELL, 1, OrderType.STOP_LIMIT, D(11), D(10))
    with pytest.raises(ValueError, match="limit_price"):
        Order("o", "c", Side.BUY, 1, OrderType.LIMIT, D(0))
    with pytest.raises(ValueError, match="only a STOP_LIMIT"):
        Order("o", "c", Side.BUY, 1, OrderType.LIMIT, D(1), D(1))
    with pytest.raises(TypeError, match="OrderType"):
        Order("o", "c", Side.BUY, 1, "MARKET", D(1))  # type: ignore[arg-type]


# --- Diego: NB-03 ---------------------------------------------------------------


def no_orders_sent(stack: Stack) -> bool:
    return not any(line.startswith("placed") for line in stack.broker.log)


def test_nb03_a_copied_or_foreign_approval_is_refused(stack: Stack, bars: list) -> None:  # type: ignore[type-arg]
    with pytest.raises(EntryRefused, match="not an approval issued"):
        stack.executor.submit(replace(stack.approved), (), SEND)
    assert no_orders_sent(stack)


def test_nb03_an_approval_is_used_once(stack: Stack) -> None:
    stack.executor.submit(stack.approved, (), SEND)
    with pytest.raises(EntryRefused, match="already used"):
        stack.executor.submit(stack.approved, (), SEND)


def test_nb03_a_stale_approval_is_refused(stack: Stack) -> None:
    with pytest.raises(EntryRefused, match="stale"):
        stack.executor.submit(stack.approved, (), DECISION_TIME + 6 * SEC)
    assert no_orders_sent(stack)


def test_nb03_risk_is_rechecked_at_send_time(stack: Stack) -> None:
    window = NewsBlackout("CPI", SEND, SEND + 60 * SEC)
    with pytest.raises(EntryRefused, match="NEWS_BLACKOUT"):
        stack.executor.submit(stack.approved, (window,), SEND)
    assert no_orders_sent(stack)


def test_nb03_a_kill_switch_latched_after_approval_blocks_the_send(stack: Stack) -> None:
    stack.ledger.latch_kill_switch("test")
    with pytest.raises(EntryRefused, match="KILL_SWITCH"):
        stack.executor.submit(stack.approved, (), SEND)
    assert no_orders_sent(stack)


# --- Diego: G5 entry and NB-02 ---------------------------------------------------


def posted_prices(stack: Stack, ticket: EntryTicket) -> list[Decimal]:
    return [stack.broker.status(ticket.order_id).order.limit_price]


def test_g5_entry_steps_toward_the_ask_and_never_past_the_ceiling(stack: Stack) -> None:
    plan = stack.approved.plan
    ticket = stack.executor.submit(stack.approved, (), SEND)
    seen = [stack.broker.status(ticket.order_id).order.limit_price]
    for second in range(1, 30):
        ticket = stack.executor.on_time(ticket, SEND + second * SEC)
        seen.append(stack.broker.status(ticket.order_id).order.limit_price)
    assert seen[0] == plan.limit_floor == D(151)
    assert seen[5] == D("151.25")
    assert seen[10] == plan.limit_ceiling == D("151.5")
    assert max(seen) == plan.limit_ceiling  # never above the ceiling
    assert stack.broker.status(ticket.order_id).order.quantity == plan.contracts
    assert stack.broker.status(ticket.order_id).order.contract_id == stack.contract
    ticket = stack.executor.on_time(ticket, SEND + 30 * SEC)
    assert ticket.outcome is EntryOutcome.UNFILLED
    assert stack.broker.status(ticket.order_id).state is OrderState.CANCELLED
    assert stack.ledger.open_position_id is None


def test_nb02_the_fill_and_the_resting_stop_happen_together(stack: Stack) -> None:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stack.quote(150, D("151.5"), FILLED_AT)  # the ask comes down to the ceiling
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    assert ticket.outcome is EntryOutcome.PROTECTED
    # Before any other step: the broker already holds the hard stop.
    stops = [o for o in stack.broker.working_orders() if o.order_type is OrderType.STOP_LIMIT]
    assert len(stops) == 1
    spec = stack.stops.resting_order(ticket.position_id)
    assert (stops[0].stop_price, stops[0].limit_price) == (spec.trigger, spec.limit)
    assert stops[0].side is Side.SELL
    assert stops[0].quantity == 1
    assert stack.ledger.account(SEND).position_open


def test_nb02_the_resting_stop_protects_after_the_system_dies(stack: Stack) -> None:
    position = filled_position(stack)
    trigger = stack.stops.resting_order(position.position_id).trigger
    # The software is gone: nobody calls Diego or Malik again. The market falls.
    del stack.executor, stack.manager
    stack.quote(trigger - 1, trigger, SEND + 60 * SEC)
    assert stack.broker.positions() == {}  # the broker's own stop closed it


def test_an_entry_the_broker_will_not_protect_is_flattened(stack: Stack) -> None:
    class NoStops(SimulatedBroker):
        def place(self, order: Order) -> OrderStatus:
            if order.order_type is OrderType.STOP_LIMIT:
                return OrderStatus(order, OrderState.REJECTED, 0, None)
            return super().place(order)

    broker = NoStops()
    stack.executor._broker = broker
    stack.manager._broker = broker
    stack.broker = broker
    stack.quote(150, D(151), SEND)  # the ask is at the floor: fills on placement
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position is not None
    closed = stack.manager.manage(
        ticket.position, stack.quote(150, D(151), SEND + SEC), (), SEND + SEC
    )
    assert closed is not None
    assert closed.reason is ExitReason.UNPROTECTED
    assert broker.positions() == {}


# --- Malik ------------------------------------------------------------------------


def test_a_hit_stop_closes_the_trade_at_the_bid(stack: Stack) -> None:
    position = filled_position(stack)  # entry 151.5, stop 114
    t = SEND + 10 * SEC
    closed = stack.manager.manage(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(114))
    assert closed.net_pnl == D(-755)  # -750 option value - $5 round-trip fees
    account = stack.ledger.account(t)
    assert (account.stopouts_today, account.realized_pnl_today) == (1, D(-755))
    assert not account.position_open
    assert stack.broker.positions() == {}


def test_the_trail_raises_the_resting_order(stack: Stack) -> None:
    position = filled_position(stack)  # entry 151.5
    for i, bid in enumerate((D("201.75"), D("201.75"), D("221.5"), D("221.5"))):
        t = FILLED_AT + (1 + i) * SEC
        assert stack.manager.manage(position, stack.quote(bid, bid + 1, t), (), t) is None
    resting = stack.broker.status(f"{position.position_id}-stop").order
    # Net peak (221.5 - 151.5) x 20 - 5 = 1395; keep half = 697.50; + 5 fees = 702.50
    # = 35.125 points above entry, rounded up to the tick: 186.75.
    assert resting.stop_price == D("186.75") == stack.stops.current(position.position_id).price
    assert resting.limit_price == D("186.75") - 20 * D("0.25")


@pytest.mark.parametrize(
    ("hour", "minute", "reason"),
    [(13, 55, ExitReason.CLOSE), (14, 1, ExitReason.CLOSE)],
)
def test_g8_flat_by_the_close_and_nb07_never_held_outside_prime(
    stack: Stack, hour: int, minute: int, reason: ExitReason
) -> None:
    position = filled_position(stack)
    t = datetime(2026, 9, 15, hour, minute, tzinfo=PT).astimezone(UTC)
    closed = stack.manager.manage(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert closed.reason is reason
    exit_order = stack.broker.status(f"{position.position_id}-exit-1").order
    assert exit_order.order_type is OrderType.LIMIT  # never a market order
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.CANCELLED


def test_flat_when_a_news_blackout_starts(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 120 * SEC
    window = NewsBlackout("FOMC", t, t + 3600 * SEC)
    closed = stack.manager.manage(position, stack.quote(160, 161, t), (window,), t)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS


def test_news_time_stop(stack: Stack) -> None:
    window = NewsBlackout("CPI", SEND - 120 * SEC, SEND - 60 * SEC)  # entry just after it
    position = filled_position(stack)  # entry 151.5
    up = (D("161.5"), D("162.5"))  # up $195 net: short of $250
    nine = position.entry_ts + 9 * 60 * SEC
    assert stack.manager.manage(position, stack.quote(*up, nine), (window,), nine) is None
    ten = position.entry_ts + 10 * 60 * SEC
    closed = stack.manager.manage(position, stack.quote(*up, ten), (window,), ten)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS_TIME_STOP


def test_daily_loss_counts_the_open_trade(stack: Stack) -> None:
    stack.ledger._state = replace(stack.ledger._state, realized_today=D(-1500))
    position = filled_position(stack)  # entry 151.5
    t = SEND + 30 * SEC
    # Open loss (125.5 - 151.5) x 20 - 5 = -525: -1500 - 525 = -2025, past -2000.
    closed = stack.manager.manage(position, stack.quote(D("125.5"), D("126.5"), t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.DAILY_LOSS


def test_kill_switch_flattens(stack: Stack) -> None:
    position = filled_position(stack)
    stack.ledger.latch_kill_switch("test")
    t = SEND + 30 * SEC
    closed = stack.manager.manage(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.KILL_SWITCH


def test_stale_or_missing_bids_alarm_but_leave_the_resting_stop(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 30 * SEC
    stale = stack.quote(160, 161, t - 10 * SEC)
    assert stack.manager.manage(position, stale, (), t) is None
    assert "no fresh quote" in stack.alarms[-1].message
    assert stack.manager.manage(position, stack.quote(0, 161, t), (), t) is None
    assert "vanished" in stack.alarms[-1].message
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING


def test_an_unfilled_exit_is_repriced_to_the_bid(stack: Stack) -> None:
    position = filled_position(stack)
    t = datetime(2026, 9, 15, 13, 55, tzinfo=PT).astimezone(UTC)
    seen = stack.quote(160, 161, t)
    stack.broker.on_quote(replace(seen, bid=D(159)))  # the bid drops as the exit goes in
    assert stack.manager.manage(position, seen, (), t) is None  # the sell at 160 rests
    later = t + 2 * SEC
    closed = stack.manager.manage(position, stack.quote(159, 160, later), (), later)
    assert closed is not None
    assert closed.exit_price == D(159)


def test_a_stop_filled_at_the_broker_closes_the_trade(stack: Stack) -> None:
    position = filled_position(stack)  # stop 114, limit 109
    t = SEND + 30 * SEC
    stack.quote(112, 113, t)  # the bid gaps through 114: the broker's stop sells at 112
    closed = stack.manager.manage(position, stack.quote(112, 113, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(112))


# --- reconcile (NB-16) ------------------------------------------------------------


def test_nb16_after_a_restart_anything_held_is_flattened(stack: Stack) -> None:
    filled_position(stack)
    t = SEND + 60 * SEC
    quotes = {stack.contract: stack.quote(160, 161, t)}
    # After a restart the system has no position record: the broker holds a contract.
    assert not stack.manager.reconcile(None, quotes, t)
    assert stack.broker.positions() == {}
    assert stack.broker.open_orders() == []  # the broker's own resting stop was cancelled
    assert stack.ledger.account(t).kill_switch_latched
    assert "reconcile mismatch" in stack.alarms[0].message


def test_nb16_agreement_changes_nothing(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 60 * SEC
    quotes = {stack.contract: stack.quote(160, 161, t)}
    assert stack.manager.reconcile(position, quotes, t)
    assert not stack.ledger.account(t).kill_switch_latched
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING


def test_nb16_a_stop_that_fills_during_the_cancel_is_never_sold_twice(stack: Stack) -> None:
    filled_position(stack)

    class FillsOnCancel(SimulatedBroker):
        pass

    broker = stack.broker
    original_cancel = broker.cancel

    def racing_cancel(order_id: str) -> OrderStatus:
        status = broker.status(order_id)
        if status.order.order_type is OrderType.STOP_LIMIT:
            # The stop fills at the broker a moment before the cancel arrives.
            broker._positions.pop(status.order.contract_id, None)
            filled = replace(status, state=OrderState.FILLED, filled=1, avg_price=D(114))
            broker._orders[order_id] = filled
            return filled
        return original_cancel(order_id)

    broker.cancel = racing_cancel  # type: ignore[method-assign]
    t = SEND + 60 * SEC
    stack.manager.reconcile(None, {stack.contract: stack.quote(160, 161, t)}, t)
    assert broker.positions() == {}  # flat, not short


def test_nb16_reconcile_keeps_working_until_flat(stack: Stack) -> None:
    filled_position(stack)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    stale = {stack.contract: replace(stack.quote(160, 161, t), bid=D(170), ask=D(171))}
    assert not stack.manager.reconcile(None, stale, t)
    assert stack.broker.positions() == {stack.contract: 1}  # the sell at 170 rests
    later = t + 5 * SEC
    assert not stack.manager.reconcile(None, {stack.contract: stack.quote(159, 160, later)}, later)
    assert stack.broker.positions() == {}  # re-priced to the real bid and filled
    assert stack.manager.reconcile(None, {}, later + SEC) is False  # ledger still shows it
    assert any("book_by_hand" in a.message for a in stack.alarms)
    stack.ledger.book_by_hand(later, stack.ledger.open_position_id or "", D(-100), "Andre")
    assert not stack.ledger.account(later).position_open


# --- failure paths ------------------------------------------------------------------


def test_a_ledger_failure_after_the_fill_still_leaves_the_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stack.ledger, "on_entry_filled", broken)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stack.quote(150, D("151.5"), FILLED_AT)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert any(o.order_type is OrderType.STOP_LIMIT for o in stack.broker.open_orders())


def test_a_broker_error_on_the_stop_means_unprotected_and_flattened(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = stack.broker.place

    def flaky(order: Order) -> OrderStatus:
        if order.order_type is OrderType.STOP_LIMIT:
            raise ConnectionError("gateway dropped")
        return original(order)

    monkeypatch.setattr(stack.broker, "place", flaky)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position is not None
    closed = stack.manager.manage(
        ticket.position, stack.quote(150, 152, SEND + SEC), (), SEND + SEC
    )
    assert closed is not None
    assert closed.reason is ExitReason.UNPROTECTED
    assert stack.broker.positions() == {}


def test_an_off_tick_fill_is_flattened_without_crashing(stack: Stack) -> None:
    stack.quote(150, D("150.9"), SEND)  # the fill comes back off the option tick
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNPROTECTED
    assert ticket.position is not None
    closed = stack.manager.manage(
        ticket.position, stack.quote(150, 152, SEND + SEC), (), SEND + SEC
    )
    assert closed is not None
    assert stack.broker.positions() == {}


def test_an_unprotected_position_is_flattened_even_on_a_stale_quote(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = stack.broker.place

    def no_stops(order: Order) -> OrderStatus:
        if order.order_type is OrderType.STOP_LIMIT:
            return OrderStatus(order, OrderState.REJECTED, 0, None)
        return original(order)

    monkeypatch.setattr(stack.broker, "place", no_stops)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.position is not None
    t = SEND + 30 * SEC
    stale = stack.quote(150, 152, t - 20 * SEC)
    stack.manager.manage(ticket.position, stale, (), t)
    assert ticket.position.exit_order_id is not None  # a flatten was sent, not just an alarm


def test_a_rejected_exit_halts_entries_and_is_retried(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    original = stack.broker.place
    attempts: list[str] = []

    def reject_first_exit(order: Order) -> OrderStatus:
        if "-exit-" in order.order_id:
            attempts.append(order.order_id)
            if len(attempts) == 1:
                return OrderStatus(order, OrderState.REJECTED, 0, None)
        return original(order)

    monkeypatch.setattr(stack.broker, "place", reject_first_exit)
    stack.broker._orders  # noqa: B018
    t = datetime(2026, 9, 15, 13, 55, tzinfo=PT).astimezone(UTC)
    assert stack.manager.manage(position, stack.quote(160, 161, t), (), t) is None
    assert stack.ledger.account(t).kill_switch_latched
    later = t + SEC
    closed = stack.manager.manage(position, stack.quote(160, 161, later), (), later)
    assert closed is not None
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]


def test_a_rejected_trail_raise_is_alarmed(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    position = filled_position(stack)
    monkeypatch.setattr(stack.broker, "modify", lambda *a, **k: stack.broker.status(a[0]))
    for i, bid in enumerate((D("201.75"), D("201.75"))):
        t = FILLED_AT + (1 + i) * SEC
        stack.manager.manage(position, stack.quote(bid, bid + 1, t), (), t)
    assert "did not raise the resting stop" in stack.alarms[-1].message


def test_weekly_loss_counts_the_open_trade(stack: Stack) -> None:
    stack.ledger._state = replace(stack.ledger._state, realized_week=D(-3000))
    position = filled_position(stack)
    t = SEND + 30 * SEC
    # Open (125.5 - 151.5) x 20 - 5 = -525: week -3525, past the -3500 limit.
    closed = stack.manager.manage(position, stack.quote(D("125.5"), D("126.5"), t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.WEEKLY_LOSS


def test_a_missing_ledger_file_is_an_error(stack: Stack, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="create=True"):
        Ledger(stack.profiles.risk, stack.profiles.instrument, StateStore(tmp_path / "gone.json"))


# --- ledger -----------------------------------------------------------------------


def at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=PT).astimezone(UTC)


def test_ledger_counts_resets_and_persists(stack: Stack, tmp_path: Path) -> None:
    risk, inst = stack.profiles.risk, stack.profiles.instrument
    ledger = Ledger(risk, inst, StateStore(tmp_path / "l.json"), create=True)
    ledger.on_entry_filled(at(15, 7, 0), "a", SessionKind.PRIME)
    ledger.on_trade_closed(at(15, 7, 30), "a", D(100))  # a winner is not a stop-out
    ledger.on_entry_filled(at(15, 8, 0), "b", SessionKind.PRIME)
    ledger.on_trade_closed(at(15, 8, 30), "b", D("-0.01"))  # any net loss is a stop-out
    acct = ledger.account(at(15, 9, 0))
    assert (acct.trades_today_prime, acct.stopouts_today) == (2, 1)
    assert acct.realized_pnl_today == D("99.99")
    reloaded = Ledger(risk, inst, StateStore(tmp_path / "l.json"))
    assert reloaded.account(at(15, 9, 0)) == acct
    # The overnight session is still "today": the daily breaker stays.
    assert reloaded.account(at(15, 20, 0)).stopouts_today == 1
    # The next prime open resets the day, not the week.
    fresh = reloaded.account(at(16, 4, 0))
    assert (fresh.stopouts_today, fresh.realized_pnl_today) == (0, D(0))
    assert fresh.realized_pnl_week == D("99.99")
    # A new ISO week resets the week (Monday Sept 21).
    assert reloaded.account(at(21, 4, 0)).realized_pnl_week == D(0)


def test_ledger_latches_the_kill_switch_on_drawdown(stack: Stack, tmp_path: Path) -> None:
    risk, inst = stack.profiles.risk, stack.profiles.instrument
    ledger = Ledger(risk, inst, StateStore(tmp_path / "k.json"), create=True)
    ledger.on_entry_filled(at(15, 7, 0), "a", SessionKind.PRIME)
    ledger.on_trade_closed(at(15, 7, 30), "a", -risk.bankroll * D("0.2"))
    assert ledger.account(at(15, 8, 0)).kill_switch_latched
    assert (
        Ledger(risk, inst, StateStore(tmp_path / "k.json"))
        .account(at(16, 8, 0))
        .kill_switch_latched
    )
    with pytest.raises(ValueError, match="who cleared"):
        ledger.clear_kill_switch(" ")
    ledger.clear_kill_switch("Andre")
    assert not ledger.account(at(16, 8, 0)).kill_switch_latched


def test_ledger_refuses_overlapping_positions(stack: Stack, tmp_path: Path) -> None:
    ledger = Ledger(
        stack.profiles.risk, stack.profiles.instrument, StateStore(tmp_path / "o.json"), create=True
    )
    ledger.on_entry_filled(at(15, 7, 0), "a", SessionKind.PRIME)
    with pytest.raises(RuntimeError, match="is open"):
        ledger.on_entry_filled(at(15, 7, 1), "b", SessionKind.PRIME)
    with pytest.raises(RuntimeError, match="open position"):
        ledger.on_trade_closed(at(15, 7, 2), "b", D(1))


# --- Josephine --------------------------------------------------------------------


def test_journal_records_decisions_and_trades(stack: Stack, bars: list, tmp_path: Path) -> None:  # type: ignore[type-arg]
    journal = Journal(tmp_path / "journal")
    decision = stack.pipeline.decide(
        bars, None, stack.ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    journal.record_decision(decision)
    position = filled_position(stack)
    t = SEND + 10 * SEC
    closed = stack.manager.manage(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    journal.record_trade(position, closed, decision)
    (d,) = journal.decisions()
    assert d["fired"] is False
    assert "contract: no option chain" in d["reasons"]
    assert {g["name"] for g in d["gates"]} >= {"trend_has_direction", "vwap_side"}
    (trade,) = journal.trades()
    assert trade["exit_reason"] == "STOP"
    assert D(trade["net_pnl"]) == D(-755)
    assert D(trade["entry_price"]) == D("151.5")
    assert [r["agent_id"] for r in trade["birth"]["reads"]][:2] == ["trend", "momentum"]


def test_nb03_an_approval_from_the_future_is_refused(stack: Stack) -> None:
    with pytest.raises(EntryRefused, match="stale"):
        stack.executor.submit(stack.approved, (), DECISION_TIME - SEC)
    assert no_orders_sent(stack)


def test_g5_at_most_max_steps_even_below_the_ceiling(
    stack: Stack,
    bars: list,  # type: ignore[type-arg]
) -> None:
    from footprint.core.options import OptionChain

    wide = chain_for(bars[-1].close)
    quotes = tuple(replace(q, bid=D("146.5"), ask=D("153.5")) for q in wide.quotes)
    chain = OptionChain(wide.underlying, wide.ts, wide.underlying_price, quotes)
    decision = stack.pipeline.decide(
        bars, chain, stack.ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    assert decision.approved is not None, decision.reasons()
    plan = decision.approved.plan
    assert (plan.limit_floor, plan.limit_ceiling) == (D(150), D("151.75"))
    stack.broker.on_quote(
        replace(
            stack.quote(150, 152, DECISION_TIME),
            contract_id=plan.choice.quote.contract_id,
            bid=D("146.5"),
            ask=D("153.5"),
        )
    )
    ticket = stack.executor.submit(decision.approved, (), SEND)
    seen = []
    for second in range(0, 30):
        ticket = stack.executor.on_time(ticket, SEND + second * SEC)
        seen.append(stack.broker.status(ticket.order_id).order.limit_price)
    assert max(seen) == plan.limit_floor + 4 * D("0.25")  # four steps, then it waits


def test_g1_watchdog_flattens_when_the_bid_gaps_through_the_stop_limit(stack: Stack) -> None:
    position = filled_position(stack)  # stop 114, resting limit 109
    t = SEND + 30 * SEC
    gap = stack.quote(108, 110, t)  # the broker's stop triggers but its 109 limit can't fill
    assert stack.broker.positions() == {stack.contract: 1}
    closed = stack.manager.manage(position, gap, (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.GAP, D(108))
    assert closed.net_pnl == (D(108) - D("151.5")) * 20 - 5
    assert stack.broker.positions() == {}


def test_the_trading_day_turns_over_at_the_prime_open(stack: Stack, tmp_path: Path) -> None:
    ledger = Ledger(
        stack.profiles.risk, stack.profiles.instrument, StateStore(tmp_path / "d.json"), create=True
    )
    ledger.on_entry_filled(at(15, 7, 0), "a", SessionKind.PRIME)
    ledger.on_trade_closed(at(15, 7, 30), "a", D(-800))
    assert ledger.account(at(16, 3, 59)).stopouts_today == 1  # still dark before 04:00
    assert ledger.account(at(16, 4, 0)).stopouts_today == 0


def test_an_exit_the_broker_cancels_is_replaced(stack: Stack) -> None:
    position = filled_position(stack)
    t = datetime(2026, 9, 15, 13, 55, tzinfo=PT).astimezone(UTC)
    seen = stack.quote(160, 161, t)
    stack.broker.on_quote(replace(seen, bid=D(159)))
    assert stack.manager.manage(position, seen, (), t) is None  # exit-1 rests at 160
    stack.broker.cancel(f"{position.position_id}-exit-1")  # the broker drops it
    later = t + SEC
    closed = stack.manager.manage(position, stack.quote(159, 160, later), (), later)
    assert closed is not None
    assert position.exit_order_id == f"{position.position_id}-exit-2"
    assert stack.ledger.account(later).kill_switch_latched


# --- review round 2 ------------------------------------------------------------------


def test_a_ledger_failure_halts_further_entries(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    bars: list,  # type: ignore[type-arg]
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stack.ledger, "on_entry_filled", broken)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert stack.halt.tripped
    # The ledger thinks nothing is open, so the pipeline would approve again ...
    again = stack.pipeline.decide(
        bars, chain_for(bars[-1].close, SEND), stack.ledger.account(SEND), (), SEND
    )
    assert again.approved is not None
    # ... but nothing more is sent.
    with pytest.raises(EntryRefused, match="halted"):
        stack.executor.submit(again.approved, (), SEND + SEC)
    assert stack.broker.positions() == {stack.contract: 1}


def test_a_stop_that_arrived_without_an_answer_is_adopted(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = stack.broker.place

    def lost_reply(order: Order) -> OrderStatus:
        status = original(order)
        if order.order_type is OrderType.STOP_LIMIT:
            raise TimeoutError("no acknowledgement")  # but the broker has it
        return status

    monkeypatch.setattr(stack.broker, "place", lost_reply)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert ticket.position is not None
    assert ticket.position.stop_order_id == f"{ticket.position_id}-stop"


def test_an_orphan_stop_is_cancelled_before_an_unprotected_exit(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_place = stack.broker.place
    original_open = stack.broker.open_orders

    def lost_reply(order: Order) -> OrderStatus:
        status = original_place(order)
        if order.order_type is OrderType.STOP_LIMIT:
            raise TimeoutError("no acknowledgement")
        return status

    calls = {"n": 0}

    def blind_once() -> list[Order]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("cannot list orders right now")
        return original_open()

    monkeypatch.setattr(stack.broker, "place", lost_reply)
    monkeypatch.setattr(stack.broker, "open_orders", blind_once)
    stack.quote(150, D(151), SEND)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.outcome is EntryOutcome.UNPROTECTED  # Diego could not confirm the stop
    assert ticket.position is not None
    t = SEND + SEC
    closed = stack.manager.manage(ticket.position, stack.quote(150, 152, t), (), t)
    assert closed is not None
    stack.quote(110, 111, t + SEC)  # a drop through an orphan stop would sell again
    assert stack.broker.positions() == {}  # flat, not short


def test_nb16_a_working_entry_after_a_restart_is_a_mismatch(stack: Stack) -> None:
    stack.executor.submit(stack.approved, (), SEND)  # resting at 151, not filled
    t = SEND + 5 * SEC
    assert not stack.manager.reconcile(None, {stack.contract: stack.quote(150, 152, t)}, t)
    assert stack.broker.open_orders() == []
    stack.quote(140, D(150), t + SEC)  # the old entry can no longer fill
    assert stack.broker.positions() == {}


def test_nb16_books_the_real_fill_not_an_estimate(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 30 * SEC
    stack.quote(112, 113, t)  # the broker's stop fills at 112 while nobody watches
    assert not stack.manager.reconcile(position, {stack.contract: stack.quote(160, 161, t)}, t)
    acct = stack.ledger.account(t)
    assert acct.realized_pnl_today == (D(112) - D("151.5")) * 20 - 5
    assert acct.stopouts_today == 1


def test_a_failing_trail_raise_is_retried_until_the_broker_matches(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    original = stack.broker.modify
    calls = {"n": 0}

    def flaky(order_id: str, limit: Decimal, stop: Decimal | None) -> OrderStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("gateway dropped")
        return original(order_id, limit, stop)

    monkeypatch.setattr(stack.broker, "modify", flaky)
    for i, bid in enumerate((D("201.75"), D("201.75"), D("201.75"))):
        t = FILLED_AT + (1 + i) * SEC
        stack.manager.manage(position, stack.quote(bid, bid + 1, t), (), t)
    assert any("could not raise" in a.message for a in stack.alarms)
    resting = stack.broker.status(f"{position.position_id}-stop").order
    assert resting.stop_price == stack.stops.current(position.position_id).price


def test_managing_a_closed_position_fails_loudly(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 30 * SEC
    stack.quote(112, 113, t)
    assert stack.manager.manage(position, stack.quote(112, 113, t), (), t) is not None
    with pytest.raises(RuntimeError, match="already closed"):
        stack.manager.manage(position, stack.quote(112, 113, t + SEC), (), t + SEC)


def test_a_stale_feed_at_the_close_still_flattens(stack: Stack) -> None:
    position = filled_position(stack)
    t = datetime(2026, 9, 15, 13, 55, tzinfo=PT).astimezone(UTC)
    stack.broker.on_quote(replace(stack.quote(160, 161, t), bid=D(150)))  # market moved away
    stale = replace(stack.quote(150, 161, t), bid=D(160), ts=t - 60 * SEC)
    assert stack.manager.manage(position, stale, (), t) is None
    first = stack.broker.status(f"{position.position_id}-exit-1").order.limit_price
    assert first == D(160)
    stack.manager.manage(position, stale, (), t + SEC)  # still stale: walk down a tick
    assert stack.broker.status(f"{position.position_id}-exit-1").order.limit_price == D("159.75")


def test_nb16_no_new_order_while_cancels_are_unconfirmed(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled_position(stack)
    monkeypatch.setattr(stack.broker, "cancel", lambda order_id: stack.broker.status(order_id))
    t = SEND + 60 * SEC
    assert not stack.manager.reconcile(None, {stack.contract: stack.quote(160, 161, t)}, t)
    assert not any(o.order_id.startswith("reconcile-") for o in stack.broker.open_orders())
    assert "waiting for cancels" in " ".join(a.message for a in stack.alarms)
    assert stack.halt.tripped
