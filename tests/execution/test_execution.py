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
    exit_order = stack.broker.status(f"{position.position_id}-exit").order
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
    exit_quote = stack.quote(160, 161, t)
    # Make the exit miss: the bid drops just as the exit is placed.
    stack.broker.on_quote(replace(exit_quote, bid=D(159), ts=t))
    closed = stack.manager.manage(position, replace(exit_quote, bid=D(160)), (), t)
    assert closed is None or closed.exit_price <= D(160)
    if closed is None:
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


def test_nb16_a_mismatch_flattens_alarms_and_latches(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 60 * SEC
    quotes = {stack.contract: stack.quote(160, 161, t)}
    # After a restart the system believes it is flat; the broker holds a contract.
    agreed = stack.manager.reconcile({}, quotes, stack.broker.working_orders(), t)
    assert not agreed
    assert stack.broker.positions() == {}
    assert stack.broker.working_orders() == []
    assert stack.ledger.account(t).kill_switch_latched
    assert "reconcile mismatch" in stack.alarms[-1].message
    assert position.position_id


def test_nb16_agreement_changes_nothing(stack: Stack) -> None:
    filled_position(stack)
    t = SEND + 60 * SEC
    quotes = {stack.contract: stack.quote(160, 161, t)}
    assert stack.manager.reconcile({stack.contract: 1}, quotes, stack.broker.working_orders(), t)
    assert not stack.ledger.account(t).kill_switch_latched


# --- ledger -----------------------------------------------------------------------


def at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=PT).astimezone(UTC)


def test_ledger_counts_resets_and_persists(stack: Stack, tmp_path: Path) -> None:
    risk, inst = stack.profiles.risk, stack.profiles.instrument
    ledger = Ledger(risk, inst, StateStore(tmp_path / "l.json"))
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
    ledger = Ledger(risk, inst, StateStore(tmp_path / "k.json"))
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
    ledger = Ledger(stack.profiles.risk, stack.profiles.instrument, StateStore(tmp_path / "o.json"))
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


def test_the_software_stop_exits_even_if_the_broker_has_not_triggered(stack: Stack) -> None:
    from footprint.execution.orders import Quote

    position = filled_position(stack)  # stop 114
    t = SEND + 30 * SEC
    seen_only_by_us = Quote(stack.contract, D(114), D(116), t)  # the broker missed it
    closed = stack.manager.manage(position, seen_only_by_us, (), t)
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.CANCELLED
    exit_order = stack.broker.status(f"{position.position_id}-exit").order
    assert exit_order.order_type is OrderType.LIMIT
    assert exit_order.limit_price == D(114)
    assert closed is not None
    assert closed.reason is ExitReason.STOP


def test_the_trading_day_turns_over_at_the_prime_open(stack: Stack, tmp_path: Path) -> None:
    ledger = Ledger(stack.profiles.risk, stack.profiles.instrument, StateStore(tmp_path / "d.json"))
    ledger.on_entry_filled(at(15, 7, 0), "a", SessionKind.PRIME)
    ledger.on_trade_closed(at(15, 7, 30), "a", D(-800))
    assert ledger.account(at(16, 3, 59)).stopouts_today == 1  # still dark before 04:00
    assert ledger.account(at(16, 4, 0)).stopouts_today == 0
