"""IbkrBroker against a fake IBKR that misbehaves the way IBKR can."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.brokers.ibkr.adapter import BrokerUnavailable, ContractBook, IbkrBroker, IbkrSettings
from footprint.brokers.ibkr.gateway import IbContract, IbOrderRequest
from footprint.execution.market import QuoteBook
from footprint.execution.orders import (
    DuplicateOrderId,
    Order,
    OrderState,
    OrderType,
    Quote,
    Side,
    TrackedBroker,
)
from tests.brokers.fake_ibkr import FakeIbkr

D = Decimal
T = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
C = "CALL23725-20260916"
FAST = timedelta(milliseconds=100)


def contract(con_id: int, strike: int = 23725) -> IbContract:
    return IbContract(con_id, "NQ", "FOP", "CME", "USD", "20260916", D(strike), "C", "20", "Q3C")


@dataclass
class Ib:
    book: QuoteBook
    contracts: ContractBook
    fake: FakeIbkr
    adapter: IbkrBroker
    tracked: TrackedBroker
    alarms: list[str]
    resyncs: list[int]
    t: datetime = T

    def quote(self, bid: float | int, ask: float | int) -> None:
        self.t += timedelta(seconds=1)
        self.book.publish(Quote(C, D(str(bid)), D(str(ask)), self.t))


def make(*, connect: bool = True, fake: FakeIbkr | None = None) -> Ib:
    book = fake.book if fake else QuoteBook()
    contracts = fake.contracts if fake else ContractBook()
    if fake is None:
        contracts.register(C, contract(501))
        fake = FakeIbkr(book, contracts)
        book.publish(Quote(C, D(150), D(152), T))
    alarms: list[str] = []
    resyncs: list[int] = []
    adapter = IbkrBroker(
        fake,
        contracts,
        IbkrSettings(FAST, FAST),
        ("NQ", "FOP"),
        alarms.append,
        lambda: resyncs.append(1),
    )
    fake.attach(adapter)
    if connect:
        fake.connect(next_valid_id=100)
    return Ib(book, contracts, fake, adapter, TrackedBroker(adapter), alarms, resyncs)


@pytest.fixture
def ib() -> Ib:
    return make()


def buy(order_id: str = "p1-entry", price: int = 151) -> Order:
    return Order(order_id, C, Side.BUY, 1, OrderType.LIMIT, D(price))


def sell(order_id: str, price: int = 200, group: str | None = None) -> Order:
    return Order(order_id, C, Side.SELL, 1, OrderType.LIMIT, D(price), oca_group=group)


# --- readiness ------------------------------------------------------------------------


def test_nothing_is_sent_or_trusted_before_the_first_resync() -> None:
    ib = make(connect=False)
    for call in (
        lambda: ib.adapter.place(buy()),
        ib.adapter.positions,
        ib.adapter.open_orders,
        lambda: ib.adapter.status("x"),
    ):
        with pytest.raises(BrokerUnavailable):
            call()
    assert ib.fake.sends == []
    ib.fake.connect(next_valid_id=100)
    assert ib.resyncs == [1]  # the runner is told to reconcile
    assert ib.adapter.positions() == {}


def test_a_lost_connection_stops_everything_until_resynced(ib: Ib) -> None:
    ib.fake.lose()
    with pytest.raises(BrokerUnavailable):
        ib.adapter.place(buy())
    assert ib.fake.sends == []
    assert any("connection lost" in a for a in ib.alarms)
    ib.fake.restore(data_lost=True)
    assert ib.resyncs == [1, 1]
    assert ib.adapter.place(buy()).state is OrderState.WORKING


# --- the order path -----------------------------------------------------------------------


def test_an_order_goes_out_with_our_id_as_its_reference(ib: Ib) -> None:
    status = ib.adapter.place(buy())
    assert status.state is OrderState.WORKING
    (request,) = ib.fake.sends
    assert isinstance(request, IbOrderRequest)
    assert (request.order_ref, request.action, request.order_type, request.tif) == (
        "p1-entry",
        "BUY",
        "LMT",
        "DAY",
    )
    assert request.ib_id == 100
    assert request.oca_group is None
    assert request.oca_type is None


def test_a_fill_is_counted_from_the_execution(ib: Ib) -> None:
    ib.adapter.place(buy())
    ib.quote(150, 151)  # the ask comes to the limit
    status = ib.adapter.status("p1-entry")
    assert (status.state, status.filled, status.avg_price) == (OrderState.FILLED, 1, D(151))
    assert ib.adapter.positions() == {C: 1}


def test_a_group_goes_out_as_one_cancels_all_with_block(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", group="p1"))
    request = ib.fake.sends[-1]
    assert (request.oca_group, request.oca_type) == ("p1", 1)


def test_an_unknown_contract_is_never_sent(ib: Ib) -> None:
    with pytest.raises(KeyError):
        ib.adapter.place(Order("x", "PUT1-20260916", Side.BUY, 1, OrderType.LIMIT, D(1)))
    assert ib.fake.sends == []


@pytest.mark.parametrize("code", [201, 110])
def test_a_rejection_is_a_rejected_status(ib: Ib, code: int) -> None:
    ib.fake.reject_next = code
    assert ib.adapter.place(buy()).state is OrderState.REJECTED


def test_an_unknown_error_on_a_send_is_not_called_a_rejection(ib: Ib) -> None:
    ib.fake.reject_next = 399
    with pytest.raises(RuntimeError, match="399"):
        ib.adapter.place(buy())


# --- lost answers ---------------------------------------------------------------------------


def test_a_lost_answer_is_resolved_by_the_resend_without_a_second_order(ib: Ib) -> None:
    ib.fake.lose_answers = 1
    status = ib.tracked.place(buy())  # first answer lost; the resend is answered
    assert status.state is OrderState.WORKING
    assert len(ib.fake.copies) == 1  # one order at IBKR
    assert {r.ib_id for r in ib.fake.sends} == {100}  # the resend used the same id


def test_a_lost_answer_on_an_order_that_filled_meanwhile(ib: Ib) -> None:
    ib.fake.lose_answers = 1
    status = ib.tracked.place(buy(price=152))  # marketable: fills at once; answer lost
    assert status.state is OrderState.WORKING  # 104 proved it arrived; fate not yet known
    assert len(ib.fake.copies) == 1
    ib.fake.release()
    assert ib.adapter.status("p1-entry").state is OrderState.FILLED
    assert ib.adapter.positions() == {C: 1}


def test_a_send_that_never_arrived_is_placed_by_the_resend(ib: Ib) -> None:
    ib.fake.drop_sends = 1
    assert ib.tracked.place(buy()).state is OrderState.WORKING
    assert len(ib.fake.copies) == 1


def test_when_both_sends_are_lost_the_order_stays_followed(ib: Ib) -> None:
    ib.fake.drop_sends = 2
    with pytest.raises(TimeoutError):
        ib.tracked.place(buy())
    assert "p1-entry" in ib.tracked.unfinished


# --- duplicate copies (a modify during degraded connectivity) --------------------------


def test_a_second_live_copy_is_alarmed_listed_and_cancelled_broker_wide(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)  # IBKR keeps the first, adds a copy
    assert len(ib.fake.copies) == 2
    assert any("DUPLICATE LIVE ORDER" in a for a in ib.alarms)
    listed = {o.order_id for o in ib.adapter.open_orders()}
    assert len(listed) == 2
    assert "p1-exit-1" in listed
    assert any("#" in i for i in listed)
    assert len(ib.tracked.working()) == 2  # both count as working (and covering)
    ib.adapter.cancel("p1-exit-1")
    assert ib.fake.global_cancels == 1  # an order-id cancel would reach only one
    assert ib.adapter.open_orders() == []


def test_two_copies_in_one_group_can_never_both_fill(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-exit-1", price=160, group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(159), None)
    ib.quote(170, 171)  # both copies would sell
    assert ib.adapter.positions() == {}  # flat, never short


# --- positions -------------------------------------------------------------------------------


def test_a_lagging_position_report_is_never_ahead_of_what_both_agree(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # filled; position report not yet sent
    ib.fake.position_lag = True
    ib.adapter.place(sell("p1-exit-1", price=150))  # sold: fills say flat
    assert ib.fake.told_positions == {501: 1}  # the report still says long 1 (lagging)
    assert ib.adapter.positions() == {}  # fills say 0, report says 1: agreed 0
    ib.fake.flush_positions()
    assert ib.adapter.positions() == {}


def test_fills_ahead_of_the_report_count_only_what_both_agree(ib: Ib) -> None:
    ib.fake.position_lag = True
    ib.adapter.place(buy(price=152))  # fills say 1; the report says nothing yet
    assert ib.adapter.positions() == {}
    time.sleep(FAST.total_seconds() * 1.5)
    ib.adapter.positions()
    assert sum("disagree" in a for a in ib.alarms) == 1
    ib.fake.flush_positions()
    assert ib.adapter.positions() == {C: 1}


def test_an_execution_replayed_on_resync_is_counted_once(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))
    assert ib.adapter.positions() == {C: 1}
    ib.fake.lose()
    ib.fake.restore(data_lost=True)  # executions are replayed; positions resnapshotted
    assert ib.adapter.positions() == {C: 1}


# --- restarts and hand-placed orders ---------------------------------------------------------


def test_after_a_restart_open_orders_are_matched_back_by_reference(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))  # rests above the market
    restarted = make(fake=ib.fake)  # a new process: a new adapter, the same IBKR
    assert restarted.adapter.status("p1-stop").state is OrderState.WORKING
    assert restarted.adapter.positions() == {C: 1}
    assert {o.order_id for o in restarted.adapter.open_orders()} == {"p1-stop"}


def test_a_hand_placed_order_is_listed_and_cancelled_broker_wide(ib: Ib) -> None:
    hand = IbOrderRequest(7, contract(501), "SELL", 1, "LMT", D(300), None, "", None, None, "DAY")
    ib.fake._new_copy(hand)  # placed in TWS: no orderRef
    listed = [o.order_id for o in ib.adapter.open_orders()]
    assert listed == [f"ib-{ib.fake._perm}"]
    ib.adapter.cancel(listed[0])
    assert ib.fake.global_cancels == 1
    assert ib.adapter.open_orders() == []


def test_a_resend_of_a_finished_order_proves_it_exists(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # filled
    with pytest.raises(DuplicateOrderId):
        ib.adapter.place(buy(price=152))  # same id again: 104


# --- the rest of the account is not the system's ------------------------------------------

STOCK = "STK-XYZ"


def with_a_stock(ib: Ib) -> None:
    """Andre's own stock in the account: held, plus a resting sell he placed by hand."""
    ib.contracts.register(
        STOCK, IbContract(900, "XYZ", "STK", "SMART", "USD", "", D(1), "", "1", "XYZ")
    )
    ib.book.publish(Quote(STOCK, D(10), D(11), ib.t))
    for ref, action, price in (("", "BUY", 11), ("", "SELL", 99)):
        ib.fake._new_copy(
            IbOrderRequest(
                0, ib.contracts.ib(STOCK), action, 5, "LMT", D(price), None, ref, None, None, "DAY"
            )
        )


def test_another_instrument_is_invisible_to_the_system(ib: Ib) -> None:
    with_a_stock(ib)
    assert ib.fake.sim.positions() == {STOCK: 5}  # really held, really working
    assert ib.adapter.positions() == {}  # but none of the system's business
    assert ib.adapter.open_orders() == []
    ib.fake.lose()
    ib.fake.restore(data_lost=True)  # and after a resync, still not
    assert ib.adapter.positions() == {}
    assert ib.adapter.open_orders() == []


def test_no_broker_wide_cancel_while_other_orders_are_open(ib: Ib) -> None:
    with_a_stock(ib)
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)  # a duplicate copy needs a global cancel
    ib.adapter.cancel("p1-exit-1")
    assert ib.fake.global_cancels == 0  # it would have cancelled Andre's stock order
    assert any("CANCEL BY HAND" in a for a in ib.alarms)
    stock_orders = [c for c in ib.fake.copies.values() if c.request.contract.sec_type == "STK"]
    assert any(ib.fake.sim.status(c.sim_id).state is OrderState.WORKING for c in stock_orders)


# --- review of this file's own mutants ------------------------------------------------------


def test_a_modify_that_arrives_after_an_unseen_fill_is_not_an_error(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", price=160))
    ib.fake.mute_reports = True  # the fill is not told yet
    ib.quote(161, 162)
    status = ib.adapter.modify("p1-exit-1", D(159), None)  # IBKR answers 104: no raise
    assert status.order.order_id == "p1-exit-1"
    ib.fake.mute_reports = False
    ib.fake._report()
    assert ib.adapter.status("p1-exit-1").state is OrderState.FILLED


def test_a_brief_disagreement_is_not_alarmed(ib: Ib) -> None:
    ib.fake.position_lag = True
    ib.adapter.place(buy(price=152))
    ib.adapter.positions()
    ib.fake.flush_positions()
    ib.adapter.positions()
    assert not any("disagree" in a for a in ib.alarms)


def test_a_fill_told_twice_is_counted_once(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))
    execution = ib.fake.executions[-1]
    ib.adapter.on_execution(*execution)  # IBKR repeats an execDetails
    assert ib.adapter.positions() == {C: 1}
    time.sleep(FAST.total_seconds() * 1.5)
    ib.adapter.positions()
    assert not any("disagree" in a for a in ib.alarms)  # the fills were not counted twice


def test_a_fill_replayed_after_the_position_snapshot_is_not_counted_twice(ib: Ib) -> None:
    """IBKR does not promise the order of a resync's answers: positions may come
    first, then the replay of a fill that the snapshot already includes."""
    ib.adapter.place(sell("p1-exit-1", price=160))  # rests
    ib.fake.lose()
    ib.quote(161, 162)  # fills while disconnected: nothing is told
    ib.fake.defer_executions = True
    ib.fake.restore(data_lost=True)  # snapshot first...
    ib.fake.flush_executions()  # ...then the replayed fill
    assert ib.adapter.positions() == {C: -1}
    time.sleep(FAST.total_seconds() * 1.5)
    ib.adapter.positions()
    assert not any("disagree" in a for a in ib.alarms)  # counted once, not on top


def test_nothing_is_usable_until_every_part_of_the_resync_is_in(ib: Ib) -> None:
    ib.fake.lose()
    ib.fake.defer_executions = True
    ib.fake.restore(data_lost=True)  # orders and positions in; executions not yet
    with pytest.raises(BrokerUnavailable):
        ib.adapter.positions()
    with pytest.raises(BrokerUnavailable):
        ib.adapter.place(buy())
    ib.fake.flush_executions()
    assert ib.adapter.positions() == {}


def test_other_instruments_orders_are_relearned_on_every_resync(ib: Ib) -> None:
    with_a_stock(ib)
    stock = next(
        c
        for c in ib.fake.copies.values()
        if c.request.contract.sec_type == "STK"
        and ib.fake.sim.status(c.sim_id).state is OrderState.WORKING
    )
    ib.fake.lose()
    ib.fake.sim.cancel(stock.sim_id)  # Andre cancels it by hand while we are cut off
    stock.sent = (OrderState.CANCELLED, 0)  # and IBKR never tells that status again
    ib.fake.restore(data_lost=True)
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)
    ib.adapter.cancel("p1-exit-1")
    assert ib.fake.global_cancels == 1  # nothing of Andre's is open any more
