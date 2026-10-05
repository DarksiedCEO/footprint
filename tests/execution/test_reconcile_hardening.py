"""PR9: reconcile on the order path — lagging position reports, stuck cancels, stale
prices, and alarm noise. Each rule here is a way a live broker differs from the
simulator; each test makes the simulator misbehave that way on purpose."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.orders import Order, OrderState, OrderStatus, OrderType, Side
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position
from tests.execution.test_runner import open_trade, runner_for

D = Decimal
SEC = timedelta(seconds=1)


def settle(stack: Stack) -> timedelta:
    return timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)


def reconcile_orders(stack: Stack) -> list[OrderStatus]:
    return [
        stack.broker.status(line.split()[1])
        for line in stack.broker.log
        if line.startswith("placed reconcile-")
    ]


# --- a position report that lags the fills ---------------------------------------------


def test_a_lagging_position_report_after_a_stop_out_is_waited_out_not_sold(
    stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop fills; for a moment the broker still reports the contract. Reconcile
    must wait for the report to catch up, not sell a contract that is already gone."""
    runner, _, alarms = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    contract = stack.contract
    drop = fill + 30 * SEC
    stack.quote(114, 116, drop)  # the resting stop fills
    lagging = {"on": True}
    real = stack.broker.positions

    def report() -> dict[str, int]:
        return {contract: 1} if lagging["on"] else real()

    monkeypatch.setattr(stack.broker, "positions", report)
    runner.step(drop)  # management books the stop-out, then doubts
    assert runner.position is None
    runner.step(drop + SEC)  # reconcile sees the stale report: waits
    lagging["on"] = False  # the report catches up inside the settle wait
    runner.step(drop + 2 * SEC)
    assert not runner.reconciling  # agreed
    assert reconcile_orders(stack) == []  # nothing was sold
    assert real() == {}  # flat, never short
    assert not stack.halt.tripped
    assert not any("reconcile mismatch" in a.message for a in alarms)


def test_a_lagging_report_after_a_cancel_race_never_sells_short(stack: Stack) -> None:
    """Reconcile cancels the stop, which fills in the race; the report lags one read.
    What is held must hold still before anything is sold."""
    filled_position(stack)
    broker = stack.broker
    contract = stack.contract
    reads = {"stale": 0}
    real_positions = broker.positions
    real_cancel = broker.cancel

    def racing_cancel(order_id: str) -> OrderStatus:
        status = broker.status(order_id)
        if status.order.order_type is OrderType.STOP_LIMIT:
            broker._positions.pop(contract, None)  # filled at the exchange first
            filled = OrderStatus(status.order, OrderState.FILLED, 1, D(114))
            broker._orders[order_id] = filled
            reads["stale"] = 1  # the next position read still shows it
            return filled
        return real_cancel(order_id)

    def report() -> dict[str, int]:
        if reads["stale"] > 0:
            reads["stale"] -= 1
            return {contract: 1}
        return real_positions()

    broker.cancel = racing_cancel  # type: ignore[method-assign]
    broker.positions = report  # type: ignore[method-assign]
    t = FILLED_AT + 60 * SEC
    stack.quote(160, 161, t)
    for k in range(6):
        stack.manager.reconcile(None, t + k * SEC)
        assert all(qty >= 0 for qty in real_positions().values())
    assert reconcile_orders(stack) == []
    assert real_positions() == {}


# --- cancels that never confirm ----------------------------------------------------------


def stuck_cancels(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack.broker, "cancel", lambda order_id: stack.broker.status(order_id))


def test_a_stuck_stop_cancel_escalates_once_and_never_sells_beside_it(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled_position(stack)
    stuck_cancels(stack, monkeypatch)
    t = FILLED_AT + 60 * SEC
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for k in range(escalate + 10):
        now = t + k * SEC
        stack.quote(160, 161, now)
        stack.manager.reconcile(None, now)
    # The stuck stop still covers the one contract held: no second seller, ever.
    assert reconcile_orders(stack) == []
    assert stack.broker.positions() == {stack.contract: 1}
    messages = [a.message for a in stack.alarms]
    assert sum("CANCEL STUCK" in m for m in messages) == 1
    assert sum("reconcile mismatch" in m for m in messages) == 1  # once, not per round
    assert sum("waiting for cancels" in m for m in messages) == 1
    assert stack.halt.tripped


def test_no_escalation_before_the_limit(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    filled_position(stack)
    stuck_cancels(stack, monkeypatch)
    t = FILLED_AT + 60 * SEC
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    first_action = t + settle(stack)  # the cancels start once the mismatch settles
    for k in range(int((first_action - t).total_seconds()) + escalate):
        stack.manager.reconcile(None, t + k * SEC)
    assert not any("CANCEL STUCK" in a.message for a in stack.alarms)


def test_what_a_stuck_order_cannot_cover_is_still_sold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck BUY on the contract covers nothing: the held contract is still sold."""
    filled_position(stack)
    stack.broker.cancel(f"{stack.ledger.open_position_id}-stop")  # (stop gone, for clarity)
    stack.broker.place(Order("andre-buy", stack.contract, Side.BUY, 1, OrderType.LIMIT, D(1)))
    stuck_cancels(stack, monkeypatch)
    t = FILLED_AT + 60 * SEC
    for k in range(6):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    assert reconcile_orders(stack)  # it sold
    assert stack.broker.positions() == {}  # flat; the stuck buy may still fill later
    assert stack.broker.status("andre-buy").state is OrderState.WORKING


def test_a_cancel_that_raises_is_chased_not_fatal(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled_position(stack)

    def unreachable(order_id: str) -> OrderStatus:
        raise ConnectionError("broker gone")

    monkeypatch.setattr(stack.broker, "cancel", unreachable)
    t = FILLED_AT + 60 * SEC
    escalate = stack.profiles.execution.reconcile.cancel_escalate_seconds
    for k in range(escalate + 10):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)  # must not raise
    assert any("CANCEL STUCK" in a.message for a in stack.alarms)
    assert stack.broker.positions() == {stack.contract: 1}  # the stop still covers it


# --- prices on a dead feed ---------------------------------------------------------------


def run_rounds(stack: Stack, start: datetime, rounds: int) -> list[Decimal]:
    """Reconcile `rounds` times a second apart on a dead feed; the prices it posted."""
    for k in range(rounds):
        stack.book.mark_time(start + k * SEC)
        stack.manager.reconcile(None, start + k * SEC)
    return [status.order.limit_price for status in reconcile_orders(stack)]


def test_on_a_dead_feed_the_flatten_walks_down_a_tick_a_round(stack: Stack) -> None:
    filled_position(stack)  # last quote: 150 / 151.5 at FILLED_AT
    t = FILLED_AT + 60 * SEC  # long stale
    prices = run_rounds(stack, t, 8)
    tick = stack.profiles.instrument.option_tick_size
    assert prices[0] == D(150)  # the last known bid
    assert prices == [D(150) - k * tick for k in range(len(prices))]
    assert len(prices) >= 3
    assert stack.broker.positions() == {stack.contract: 1}  # nothing fills on a dead quote


def test_a_fresh_quote_resets_the_walk_to_the_bid(stack: Stack) -> None:
    filled_position(stack)
    t = FILLED_AT + 60 * SEC
    run_rounds(stack, t, 6)
    later = t + 6 * SEC
    stack.quote(140, 141, later)  # the feed returns, below the walked-down limit
    assert stack.broker.positions() == {stack.contract: 1}  # so nothing filled yet
    stack.manager.reconcile(None, later)
    assert reconcile_orders(stack)[-1].order.limit_price == D(140)


def test_the_walk_never_goes_below_one_tick(stack: Stack) -> None:
    filled_position(stack)
    stack.quote(0, D("0.5"), FILLED_AT + SEC)  # no bid, then the feed dies
    prices = run_rounds(stack, FILLED_AT + 60 * SEC, 8)
    tick = stack.profiles.instrument.option_tick_size
    assert prices
    assert min(prices) == tick


def test_a_short_is_bought_back_walking_up_on_a_dead_feed(stack: Stack) -> None:
    stack.broker._positions[stack.contract] = -1  # somehow short (it must never be)
    stack.quote(150, 152, FILLED_AT)
    prices = run_rounds(stack, FILLED_AT + 60 * SEC, 8)
    tick = stack.profiles.instrument.option_tick_size
    assert prices[0] == D(152)  # the last known ask
    assert prices == [D(152) + k * tick for k in range(len(prices))]


# --- review of this file's own mutants --------------------------------------------------


def test_an_order_whose_status_cannot_be_read_still_counts_as_covering(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the broker lists a stuck sell but will not say how much of it is left, it is
    taken as wholly unfilled: reconcile must not add a second sell beside it."""
    filled_position(stack)
    stuck_cancels(stack, monkeypatch)
    real_status = stack.broker.status
    stop_id = f"{stack.ledger.open_position_id}-stop"

    def blind(order_id: str) -> OrderStatus:
        if order_id == stop_id:
            raise ConnectionError("no answer")
        return real_status(order_id)

    t = FILLED_AT + 60 * SEC
    stack.quote(160, 161, t)
    monkeypatch.setattr(stack.broker, "status", blind)
    for k in range(6):
        stack.manager.reconcile(None, t + k * SEC)
    monkeypatch.setattr(stack.broker, "status", real_status)
    assert reconcile_orders(stack) == []
    assert stack.broker.positions() == {stack.contract: 1}


def test_its_own_resting_flatten_never_re_alarms_the_mismatch(stack: Stack) -> None:
    filled_position(stack)
    run_rounds(stack, FILLED_AT + 60 * SEC, 8)  # dead feed: a new flatten order each round
    assert len(reconcile_orders(stack)) >= 3
    assert sum("reconcile mismatch" in a.message for a in stack.alarms) == 1


def test_a_new_episode_starts_fresh_after_agreement(stack: Stack) -> None:
    """Once reconcile agrees, its walk, its wait and its alarms start over: a later
    disagreement waits to settle again and prices from the last known touch."""
    filled_position(stack)
    t = FILLED_AT + 60 * SEC
    run_rounds(stack, t, 8)  # walks the price down on a dead feed
    later = t + 8 * SEC
    stack.quote(150, 151, later)  # the feed returns at 150: the walked-down sell fills
    stack.ledger.book_by_hand(later, stack.ledger.open_position_id or "", D(-1000), "Andre")
    assert stack.manager.reconcile(None, later + SEC)  # flat, ledger clear: agreed
    # A new disagreement: someone buys the contract by hand.
    stack.broker._positions[stack.contract] = 1
    alarms_before = len(stack.alarms)
    placed_before = len(reconcile_orders(stack))
    restart = later + 120 * SEC  # the feed is dead again; last bid seen was 150
    stack.book.mark_time(restart)
    assert not stack.manager.reconcile(None, restart)
    assert len(stack.alarms) == alarms_before  # waits to settle again: no alarm yet
    run_rounds(stack, restart + SEC, 6)
    fresh = reconcile_orders(stack)[placed_before:]
    assert fresh
    assert fresh[0].order.limit_price == D(150)  # from the last touch, not the old walk
