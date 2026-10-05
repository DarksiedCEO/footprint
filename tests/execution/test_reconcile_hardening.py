"""PR9: reconcile on the order path — lagging position reports, stuck cancels, stale
prices, and alarm noise. Each rule here is a way a live broker differs from the
simulator; each test makes the simulator misbehave that way on purpose."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position
from tests.execution.test_runner import open_trade, runner_for

D = Decimal
SEC = timedelta(seconds=1)


def working_sells(stack: Stack) -> int:
    return sum(
        st.order.quantity - st.filled
        for st in stack.broker._orders.values()
        if st.state is OrderState.WORKING and st.order.side is Side.SELL
    )


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


def frozen_modifies(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker accepts a modify but never applies it (as when it is unreachable)."""
    monkeypatch.setattr(stack.broker, "modify", lambda order_id, *a: stack.broker.status(order_id))


def test_a_stuck_stop_is_repriced_to_the_market_not_sold_beside(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-3: the stop will not cancel, so reconcile moves it to trigger at the bid.
    It fills; no second seller was ever sent; the account ends flat, never short."""
    filled_position(stack)
    stuck_cancels(stack, monkeypatch)
    t = FILLED_AT + 60 * SEC
    for k in range(8):
        now = t + k * SEC
        stack.quote(160, 161, now)
        stack.manager.reconcile(None, now)
    assert reconcile_orders(stack) == []
    assert stack.broker.positions() == {}
    stop = stack.broker.status(f"{stack.ledger.open_position_id}-stop")
    assert stop.state is OrderState.FILLED
    assert stop.avg_price == D(160)


def test_a_stuck_stop_cancel_escalates_once_and_never_sells_beside_it(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled_position(stack)
    stuck_cancels(stack, monkeypatch)
    frozen_modifies(stack, monkeypatch)
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
    monkeypatch.setattr(stack.broker, "modify", unreachable)
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

    frozen_modifies(stack, monkeypatch)
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


# --- a broker that lists late and reports unsteadily (review R9-1, R9-2) ----------------


class LaggyBroker(SimulatedBroker):
    """A simulator that misbehaves the way a live broker can: a newly placed order is
    left out of `open_orders` for the next `list_lag_reads` reads, and `positions`
    can flicker between the truth and nothing on alternate reads."""

    def __init__(self, book: QuoteBook, max_age: int, *, list_lag_reads: int = 0) -> None:
        super().__init__(book, max_age)
        self.list_lag_reads = list_lag_reads
        self.flicker = False
        self._hidden: dict[str, int] = {}
        self._reads = 0

    def place(self, order: Order) -> OrderStatus:
        status = super().place(order)
        if self.list_lag_reads:
            self._hidden[order.order_id] = self.list_lag_reads
        return status

    def open_orders(self) -> list[Order]:
        shown = []
        for order in super().open_orders():
            left = self._hidden.get(order.order_id, 0)
            if left > 0:
                self._hidden[order.order_id] = left - 1
                continue
            shown.append(order)
        return shown

    def positions(self) -> dict[str, int]:
        """With `flicker`, every third read reports nothing (out of step with any
        fixed number of reads per round, so every snapshot keeps changing)."""
        if self.flicker:
            self._reads += 1
            if self._reads % 3 == 0:
                return {}
        return super().positions()

    def truth(self) -> dict[str, int]:
        return dict(self._positions)

    def working_sells(self, contract_id: str) -> int:
        return sum(
            s.order.quantity - s.filled
            for s in self._orders.values()
            if s.state is OrderState.WORKING
            and s.order.side is Side.SELL
            and s.order.contract_id == contract_id
        )


def use(stack: Stack, broker: SimulatedBroker) -> None:
    stack.use_broker(broker)


def test_r9_1_a_late_listed_flatten_never_gets_a_second_seller(stack: Stack) -> None:
    broker = LaggyBroker(
        stack.book, stack.profiles.execution.quotes.max_age_seconds, list_lag_reads=4
    )
    use(stack, broker)
    filled_position(stack)  # a leftover long of 1, then a "restart"
    t = FILLED_AT + 60 * SEC  # the feed is dead: every flatten rests
    for k in range(14):
        now = t + k * SEC
        stack.book.mark_time(now)
        stack.manager.reconcile(None, now)
        assert broker.working_sells(stack.contract) <= 1  # never two sellers
    assert reconcile_orders(stack)  # it did try to flatten
    stack.quote(150, 152, t + 14 * SEC)  # a live bid: whatever rests can fill
    assert all(qty >= 0 for qty in broker.truth().values())


def test_r9_1_an_own_order_the_broker_cannot_report_yet_still_covers(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flatten reached the broker, but asking for it by id fails for a while: it
    is counted as working, so nothing is sent beside it."""
    filled_position(stack)
    real_status = stack.broker.status
    blind_for = {"reads": 0}

    def late(order_id: str) -> OrderStatus:
        if order_id.startswith("reconcile-") and blind_for["reads"] < 30:
            blind_for["reads"] += 1
            raise KeyError(order_id)
        return real_status(order_id)

    monkeypatch.setattr(stack.broker, "status", late)
    monkeypatch.setattr(
        stack.broker,
        "open_orders",
        lambda: [
            o
            for o in SimulatedBroker.open_orders(stack.broker)
            if not o.order_id.startswith("reconcile-")
        ],
    )
    t = FILLED_AT + 60 * SEC
    for k in range(10):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
        assert working_sells(stack) <= 1  # never two sellers at once


def test_r9_2_a_flickering_report_escalates_and_is_never_acted_on(stack: Stack) -> None:
    broker = LaggyBroker(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    use(stack, broker)
    filled_position(stack)
    broker.flicker = True
    t = FILLED_AT + 60 * SEC
    limit = stack.profiles.execution.reconcile.max_settle_seconds
    for k in range(limit + 20):
        now = t + k * SEC
        stack.quote(160, 161, now)
        stack.manager.reconcile(None, now)
        if k < limit:
            assert not any("NOT SETTLING" in a.message for a in stack.alarms)
    messages = [a.message for a in stack.alarms]
    assert sum("NOT SETTLING" in m for m in messages) == 1
    assert stack.halt.tripped
    assert stack.ledger.account(t).kill_switch_latched
    assert reconcile_orders(stack) == []  # nothing sold on an untrusted report
    assert broker.truth() == {stack.contract: 1}  # the resting stop still protects it


# --- this round's own mutants -------------------------------------------------------------


def test_finished_flatten_orders_stop_being_followed(stack: Stack) -> None:
    filled_position(stack)
    t = FILLED_AT + 60 * SEC
    for k in range(6):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)  # the flatten fills at 160
    assert stack.broker.positions() == {}
    assert stack.manager._broker.unfinished == {}  # nothing finished is chased forever


def test_holdings_that_will_not_settle_after_the_cancels_escalate(stack: Stack) -> None:
    broker = LaggyBroker(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    use(stack, broker)
    filled_position(stack)
    t = FILLED_AT + 60 * SEC
    settle = stack.profiles.execution.reconcile.settle_seconds
    limit = stack.profiles.execution.reconcile.max_settle_seconds
    stack.book.mark_time(t)
    for k in range(settle + 1):  # the disagreement settles; reconcile starts acting
        stack.manager.reconcile(None, t + k * SEC)
    assert stack.manager._acting
    broker.flicker = True  # now what is held keeps changing
    for k in range(settle + 1, limit + 10):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    assert sum("NOT SETTLING" in a.message for a in stack.alarms) == 1
    assert all(qty >= 0 for qty in broker.truth().values())


def test_an_order_merely_slow_to_cancel_is_not_repriced(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel confirmed one round late is normal: no modify is sent at it."""
    filled_position(stack)
    real_cancel = stack.broker.cancel
    requested: set[str] = set()

    def late_cancel(order_id: str) -> OrderStatus:
        if order_id in requested:
            return real_cancel(order_id)  # confirmed on the second ask
        requested.add(order_id)
        return stack.broker.status(order_id)

    modifies: list[str] = []
    real_modify = stack.broker.modify

    def counting(order_id: str, limit: Decimal, stop: Decimal | None) -> OrderStatus:
        modifies.append(order_id)
        return real_modify(order_id, limit, stop)

    monkeypatch.setattr(stack.broker, "cancel", late_cancel)
    monkeypatch.setattr(stack.broker, "modify", counting)
    t = FILLED_AT + 60 * SEC  # a dead feed: each flatten rests, then is re-priced
    for k in range(12):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    assert len(reconcile_orders(stack)) >= 2  # flatten orders came and went
    assert modifies == []  # each was cancelled (late) and replaced, never modified


def test_stuck_sells_are_repriced_only_up_to_what_is_held(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two stuck sells against one contract held: only one is moved to the market,
    or both would fill and leave a short."""
    filled_position(stack)
    stack.broker.place(Order("andre-sell", stack.contract, Side.SELL, 1, OrderType.LIMIT, D(200)))
    stuck_cancels(stack, monkeypatch)
    t = FILLED_AT + 60 * SEC
    for k in range(10):
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    assert stack.broker.positions() == {}  # flat via one repriced sell
    assert stack.broker.status("andre-sell").order.limit_price == D(200)  # untouched
    assert any("flatten by hand" in a.message and "would SELL 2" in a.message for a in stack.alarms)


def test_with_no_settle_wait_a_slow_cancel_is_still_not_repriced(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settle_seconds may be configured to 0; then only the one-round rule keeps a
    merely slow cancel from being modified."""
    from dataclasses import replace

    p = stack.profiles
    stack.manager._p = replace(
        p,
        execution=replace(p.execution, reconcile=replace(p.execution.reconcile, settle_seconds=0)),
    )
    filled_position(stack)
    real_cancel = stack.broker.cancel
    requested: set[str] = set()

    def late_cancel(order_id: str) -> OrderStatus:
        if order_id in requested:
            return real_cancel(order_id)
        requested.add(order_id)
        return stack.broker.status(order_id)

    modifies: list[str] = []
    real_modify = stack.broker.modify

    def counting(order_id: str, limit: Decimal, stop: Decimal | None) -> OrderStatus:
        modifies.append(order_id)
        return real_modify(order_id, limit, stop)

    monkeypatch.setattr(stack.broker, "cancel", late_cancel)
    monkeypatch.setattr(stack.broker, "modify", counting)
    t = FILLED_AT + 60 * SEC
    for k in range(8):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
    assert reconcile_orders(stack)
    assert modifies == []


# --- review R10-1: a lost exit answer on the management path -----------------------------


@pytest.mark.parametrize("resend_answers", [True, False])
def test_r10_1_a_lost_exit_answer_never_gets_a_second_exit(
    stack: Stack,
    bars: list[Bar],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resend_answers: bool,
) -> None:
    """The exit reaches the broker but its answer is lost, and the broker lists new
    orders late. Whether the resend under the same id answers (it proves the exit
    arrived) or not (its fate stays unknown), no second exit is ever working, and the
    account is never short."""
    broker = LaggyBroker(
        stack.book, stack.profiles.execution.quotes.max_age_seconds, list_lag_reads=3
    )
    stack.use_broker(broker)
    runner, _, _ = runner_for(stack, bars, tmp_path)
    fill = open_trade(stack, runner)
    real_place = SimulatedBroker.place
    exit_sends = {"n": 0}

    def lost_exit(order: Order) -> OrderStatus:
        if "-exit-" in order.order_id:
            exit_sends["n"] += 1
            if exit_sends["n"] == 1:
                real_place(broker, order)  # reaches the broker; the answer is lost
                raise TimeoutError("no acknowledgement")
            if not resend_answers:
                raise TimeoutError("no acknowledgement")
        return real_place(broker, order)

    monkeypatch.setattr(broker, "place", lost_exit)
    stack.ledger.latch_kill_switch("test")  # management starts a kill-switch exit
    for second in range(1, 15):
        t = fill + second * SEC
        stack.book.mark_time(t)  # the feed is quiet: exits rest
        runner.step(t)
        assert working_sells(stack) <= 1
    exits = [s for s in broker._orders.values() if "-exit-" in s.order.order_id]
    assert len(exits) <= 1
    stack.quote(150, 152, fill + 16 * SEC)  # a live bid: whatever rests can fill
    assert all(qty >= 0 for qty in broker.truth().values())


def test_a_report_that_settles_late_is_not_called_unsettled_afterwards(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle clock runs over one unbroken unsettled stretch: a disagreement that
    flickered for a while, then settled, does not count against the next wait (here,
    a cancel that confirms one round late once reconcile starts acting)."""
    broker = LaggyBroker(stack.book, stack.profiles.execution.quotes.max_age_seconds)
    stack.use_broker(broker)
    filled_position(stack)
    real_cancel = broker.cancel
    late = {"once": True}

    def late_once(order_id: str) -> OrderStatus:
        if late["once"]:
            late["once"] = False
            return broker.status(order_id)
        return real_cancel(order_id)

    monkeypatch.setattr(broker, "cancel", late_once)
    limit = stack.profiles.execution.reconcile.max_settle_seconds
    t = FILLED_AT + 60 * SEC
    broker.flicker = True
    k = 0
    while k < limit - 2:  # flickers for most of the limit, never all of it
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
        k += 1
    broker.flicker = False
    for _ in range(10):  # settles; reconcile acts and flattens
        stack.quote(160, 161, t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
        k += 1
    assert not any("NOT SETTLING" in a.message for a in stack.alarms)
    assert broker.truth() == {}


def test_a_long_normal_reconcile_is_not_called_unsettled(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile walks a dead feed for longer than the limit, settled every round;
    then one cancel confirms late. That short wait is not 'not settling'."""
    filled_position(stack)
    limit = stack.profiles.execution.reconcile.max_settle_seconds
    t = FILLED_AT + 60 * SEC
    k = 0
    for _ in range(limit + 5):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
        k += 1
    real_cancel = stack.broker.cancel
    late = {"once": True}

    def late_once(order_id: str) -> OrderStatus:
        if late["once"]:
            late["once"] = False
            return stack.broker.status(order_id)
        return real_cancel(order_id)

    monkeypatch.setattr(stack.broker, "cancel", late_once)
    for _ in range(6):
        stack.book.mark_time(t + k * SEC)
        stack.manager.reconcile(None, t + k * SEC)
        k += 1
    assert not any("NOT SETTLING" in a.message for a in stack.alarms)
