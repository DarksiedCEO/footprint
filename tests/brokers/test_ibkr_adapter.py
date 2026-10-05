"""IbkrBroker against a fake IBKR that misbehaves the way IBKR can."""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.brokers.ibkr.adapter import (
    BrokerUnavailable,
    ContractBook,
    IbkrBroker,
    IbkrSettings,
    PositionsInDoubt,
)
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
    ib.fake.reject_next = 404
    with pytest.raises(RuntimeError, match="404"):
        ib.adapter.place(buy())


@pytest.mark.parametrize("code", [399, 10349, 2109, 165, 10167])
def test_a_warning_is_neither_an_answer_nor_an_error(ib: Ib, code: int) -> None:
    ib.fake.warn_on_send = code
    assert ib.adapter.place(sell("p1-exit-1")).state is OrderState.WORKING
    status = ib.adapter.modify("p1-exit-1", D(199), None)
    assert status.order.limit_price == D(199)


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


def test_a_lagging_position_report_puts_positions_in_doubt(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # filled
    ib.fake.position_lag = True
    ib.adapter.place(sell("p1-exit-1", price=150))  # sold: fills say flat
    assert ib.fake.told_positions == {501: 1}  # the report still says long 1 (lagging)
    with pytest.raises(PositionsInDoubt):
        ib.adapter.positions()  # no number at all, not a guess
    ib.fake.flush_positions()
    assert ib.adapter.positions() == {}


def test_a_lasting_disagreement_is_alarmed_and_resynced(ib: Ib) -> None:
    ib.fake.position_lag = True
    ib.adapter.place(buy(price=152))  # fills say 1; the report says nothing yet
    with pytest.raises(PositionsInDoubt):
        ib.adapter.positions()
    time.sleep(FAST.total_seconds() * 1.5)
    with pytest.raises(PositionsInDoubt):
        ib.adapter.positions()
    assert sum("disagree" in a for a in ib.alarms) == 1
    assert ib.resyncs == [1, 1]  # a fresh resync, and the runner told to reconcile
    assert ib.adapter.positions() == {C: 1}  # the fresh snapshot agrees


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
    with pytest.raises(PositionsInDoubt):
        ib.adapter.positions()
    ib.fake.flush_positions()
    assert ib.adapter.positions() == {C: 1}
    assert not any("disagree" in a for a in ib.alarms)
    assert ib.resyncs == [1]


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


# --- review round 2: fills during a resync ------------------------------------------------


def test_positions_are_asked_only_after_the_execution_replay_ends(ib: Ib) -> None:
    ib.fake.lose()
    ib.fake.defer_executions = True
    asked = ib.fake.position_requests
    ib.fake.restore(data_lost=True)
    assert ib.fake.position_requests == asked  # not before every replayed fill is in
    ib.fake.flush_executions()
    assert ib.fake.position_requests == asked + 1


def test_a_fill_during_the_snapshot_is_counted_by_retaking_it(ib: Ib) -> None:
    """The review's case: an entry resting below the market fills while the
    position snapshot is being taken."""
    ib.adapter.place(buy(price=140))  # rests
    ib.fake.lose()
    ib.fake.during_snapshot = lambda: ib.quote(139, 140)  # fills mid-snapshot
    asked = ib.fake.position_requests
    ib.fake.restore(data_lost=True)
    assert ib.fake.position_requests == asked + 2  # the first snapshot was thrown away
    assert ib.adapter.status("p1-entry").state is OrderState.FILLED
    assert ib.adapter.positions() == {C: 1}  # seen, and counted once
    time.sleep(FAST.total_seconds() * 1.5)
    assert ib.adapter.positions() == {C: 1}
    assert not any("disagree" in a for a in ib.alarms)


def test_a_fill_during_the_replay_is_inside_the_snapshot(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1 (one execution to replay)
    ib.adapter.place(sell("p1-exit-1", price=160))  # rests
    ib.fake.lose()
    ib.fake.during_replay = lambda: ib.quote(161, 162)  # sells while replaying
    ib.fake.restore(data_lost=True)
    assert ib.adapter.positions() == {}
    assert ib.adapter.status("p1-exit-1").state is OrderState.FILLED


def test_a_corrected_fill_replaces_the_fill_it_corrects(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))
    ib.fake.correct(-1, 1)  # same fill, new version (".01.02")
    assert ib.adapter.positions() == {C: 1}  # not 2
    ib.adapter.on_execution(*ib.fake.executions[-1])  # the correction told twice
    assert ib.adapter.positions() == {C: 1}


def test_an_older_version_of_a_corrected_fill_is_ignored(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))
    original = ib.fake.executions[-1]
    ib.fake.correct(-1, 1)
    ib.adapter.on_execution(*original)  # the superseded version arrives late
    assert ib.adapter.positions() == {C: 1}


# --- review round 2: orders that finished unseen -----------------------------------------


def test_a_stop_cancelled_while_disconnected_is_not_a_phantom(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))  # rests
    ib.fake.lose()
    (stop,) = [c for c in ib.fake.copies.values() if c.request.order_ref == "p1-stop"]
    ib.fake.sim.cancel(stop.sim_id)  # cancelled in TWS while we were cut off
    stop.sent = (OrderState.CANCELLED, 0)  # and IBKR never tells that status
    ib.fake.restore(data_lost=True)
    status = ib.adapter.status("p1-stop")
    assert (status.state, status.filled) == (OrderState.CANCELLED, 0)
    assert ib.adapter.open_orders() == []
    assert any("finished unseen" in a for a in ib.alarms)
    assert ib.adapter.positions() == {C: 1}  # still held, and known to be unprotected


def test_a_stop_filled_while_disconnected_is_settled_from_the_fills(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-stop", price=160, group="p1"))  # rests
    ib.fake.lose()
    ib.quote(161, 162)  # fills unseen (nothing is told while cut off)
    ib.fake.restore(data_lost=True)
    status = ib.adapter.status("p1-stop")
    assert (status.state, status.filled) == (OrderState.FILLED, 1)
    assert status.avg_price is not None
    assert ib.adapter.positions() == {}


@pytest.mark.parametrize("code", [135, 161, 202])
def test_a_cancel_answered_without_a_status_is_settled_from_the_open_orders(
    ib: Ib, code: int
) -> None:
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))
    (stop,) = ib.fake.copies.values()
    ib.fake.sim.cancel(stop.sim_id)  # gone at IBKR...
    stop.sent = (OrderState.CANCELLED, 0)  # ...with no status told
    ib.fake.cancel_answer = code
    status = ib.adapter.cancel("p1-stop")
    assert status.state is OrderState.CANCELLED  # not WORKING for ever
    assert ib.adapter.open_orders() == []


def test_a_cancel_answered_not_found_leaves_a_listed_order_working(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))
    ib.fake.cancel_answer = 135  # a wrong answer: IBKR still lists it open
    status = ib.adapter.cancel("p1-stop")
    assert status.state is OrderState.WORKING  # the open orders decide, not the code


def test_a_pending_cancel_is_still_working_and_can_still_fill(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-exit-1", price=160))
    ib.fake.slow_cancels = True
    status = ib.adapter.cancel("p1-exit-1")  # IBKR: PendingCancel
    assert status.state is OrderState.WORKING  # it can still sell
    assert [o.order_id for o in ib.adapter.open_orders()] == ["p1-exit-1"]
    ib.quote(161, 162)  # it fills before the cancel lands
    ib.fake.finish_cancels()
    assert ib.adapter.status("p1-exit-1").state is OrderState.FILLED
    assert ib.adapter.positions() == {}


# --- review round 2: Inactive, modifies, 105, 1102 ----------------------------------------


def test_an_inactive_order_counts_as_working_and_is_cancelled(ib: Ib) -> None:
    ib.adapter.place(buy(price=140))  # rests
    (entry,) = ib.fake.copies.values()
    ib.fake.slow_cancels = True
    ib.fake.inactivate(entry.perm_id)
    assert any("Inactive" in a for a in ib.alarms)
    assert ib.fake.cancels == [entry.ib_id]  # the adapter cancels it at once
    assert ib.adapter.status("p1-entry").state is OrderState.WORKING  # not finished yet
    ib.fake.finish_cancels()
    assert ib.adapter.status("p1-entry").state is OrderState.CANCELLED


def test_an_inactive_order_that_comes_back_and_fills_is_counted(ib: Ib) -> None:
    ib.adapter.place(buy(price=140))
    (entry,) = ib.fake.copies.values()
    ib.fake.slow_cancels = True
    ib.fake.inactivate(entry.perm_id)
    assert ib.adapter.status("p1-entry").state is OrderState.WORKING  # never "rejected"
    ib.fake.reactivate(entry.perm_id)
    ib.quote(139, 140)  # it fills
    assert ib.adapter.status("p1-entry").state is OrderState.FILLED
    assert ib.adapter.positions() == {C: 1}


def test_an_inactive_order_is_finished_once_its_cancel_is_answered(ib: Ib) -> None:
    ib.adapter.place(buy(price=140))
    (entry,) = ib.fake.copies.values()
    ib.fake.cancel_answer = 161  # IBKR: it cannot be cancelled (it is dead)
    ib.fake.inactivate(entry.perm_id)
    assert ib.adapter.status("p1-entry").state is OrderState.REJECTED


def test_a_modify_is_not_confirmed_by_a_stale_status(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=200, group="p1"))
    ib.fake.modify_error = 105  # a status is already on its way; the refusal follows
    with pytest.raises(TimeoutError):
        ib.adapter.modify("p1-stop", D(199), None)
    assert ib.adapter.status("p1-stop").order.limit_price == D(200)  # what IBKR holds


def test_a_late_refusal_of_a_modify_raises(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=200, group="p1"))
    ib.fake.modify_error = 110
    later = threading.Timer(FAST.total_seconds() / 4, ib.fake.release)
    later.start()
    with pytest.raises(RuntimeError, match="110"):
        ib.adapter.modify("p1-stop", D("199.13"), None)
    later.join()
    assert ib.adapter.status("p1-stop").order.limit_price == D(200)


def test_status_reports_what_ibkr_holds_not_what_was_asked(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=200, group="p1"))
    ib.adapter.modify("p1-stop", D(199), None)
    assert ib.adapter.status("p1-stop").order.limit_price == D(199)  # echoed by IBKR
    assert [o.limit_price for o in ib.adapter.open_orders()] == [D(199)]


def test_a_resend_that_does_not_match_the_original_proves_it_exists(ib: Ib) -> None:
    ib.adapter.place(buy())
    ib.fake.reject_next = 105
    with pytest.raises(DuplicateOrderId):
        ib.adapter.place(buy())


def test_restored_with_data_maintained_still_resyncs(ib: Ib) -> None:
    ib.fake.lose()
    ib.fake.restore(data_lost=False)  # 1102
    assert ib.resyncs == [1, 1]
    assert ib.adapter.place(buy()).state is OrderState.WORKING


def test_an_end_of_an_older_request_does_not_end_a_newer_one(ib: Ib) -> None:
    """IBKR's ends carry no request id: with two snapshots asked, only the second
    end completes it."""
    ib.fake.lose()
    ib.fake.defer_executions = True
    ib.fake.restore(data_lost=False)  # a replay asked; its answer held back
    ib.fake.lose()
    ib.fake.restore(data_lost=False)  # asked again: two replays now outstanding
    ib.adapter.on_executions_end()  # the first of them ends
    with pytest.raises(BrokerUnavailable):
        ib.adapter.positions()  # not yet: the latest replay has not ended
    ib.fake.flush_executions()
    assert ib.adapter.positions() == {}


def test_a_resync_that_will_not_complete_is_alarmed(ib: Ib) -> None:
    ib.fake.lose()
    ib.fake.defer_executions = True
    ib.fake.restore(data_lost=True)
    time.sleep(FAST.total_seconds() * 1.5)
    with pytest.raises(BrokerUnavailable):
        ib.adapter.positions()
    with pytest.raises(BrokerUnavailable):
        ib.adapter.positions()
    assert sum("not completing" in a for a in ib.alarms) == 1


def test_an_inactive_order_whose_cancel_is_unanswered_stays_working(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", price=160))
    (exit_,) = ib.fake.copies.values()
    ib.fake.mute_cancels = True
    ib.fake.inactivate(exit_.perm_id)
    assert ib.adapter.status("p1-exit-1").state is OrderState.WORKING
    assert [o.order_id for o in ib.adapter.open_orders()] == ["p1-exit-1"]  # still covers


def test_a_new_session_forgets_requests_the_old_one_never_answered(ib: Ib) -> None:
    ib.fake.lose()
    ib.fake.defer_executions = True
    ib.fake.restore(data_lost=True)  # a replay asked; never answered
    ib.fake.lose()
    ib.fake.connect(next_valid_id=100)  # a new socket: the old request is gone
    ib.fake.flush_executions()  # the new session's replay ends once
    assert ib.adapter.positions() == {}


def test_a_report_a_moment_behind_a_fill_is_waited_for(ib: Ib) -> None:
    ib.fake.position_lag = True
    ib.adapter.place(buy(price=152))  # filled; the report follows a moment later
    later = threading.Timer(FAST.total_seconds() / 4, ib.fake.flush_positions)
    later.start()
    assert ib.adapter.positions() == {C: 1}  # waited, not raised
    later.join()


def test_an_order_taken_as_finished_that_comes_back_live_is_followed_again(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))
    (stop,) = ib.fake.copies.values()
    ib.fake.lose()
    ib.fake.connected = True  # IBKR leaves it out of the list (it is still live)
    real = ib.fake.request_open_orders
    ib.fake.request_open_orders = lambda: ib.fake._emit("on_open_orders_end", ())  # type: ignore[method-assign]
    ib.fake.restore(data_lost=True)
    ib.fake.request_open_orders = real  # type: ignore[method-assign]
    assert ib.adapter.status("p1-stop").state is OrderState.CANCELLED
    assert any("CHECK TWS" in a for a in ib.alarms)
    ib.fake._report(force=stop.ib_id)  # IBKR reports it live after all
    assert ib.adapter.status("p1-stop").state is OrderState.WORKING
    assert any("LIVE again" in a for a in ib.alarms)
    assert ib.resyncs == [1, 1, 1]  # and the runner is sent to reconcile


def test_the_open_orders_are_listed_afresh_before_a_broker_wide_cancel(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)  # a duplicate copy: needs a global cancel
    ib.fake.connected = False  # (so its events are not told)
    with_a_stock(ib)  # Andre places a stock order now, from another client: unseen
    ib.fake.connected = True
    assert ib.adapter.outside_open_orders == 0  # the adapter does not know of it yet
    ib.adapter.cancel("p1-exit-1")
    assert ib.fake.global_cancels == 0
    assert any("CANCEL BY HAND" in a for a in ib.alarms)


def test_a_fill_told_before_its_open_order_is_a_fill(ib: Ib) -> None:
    ib.fake.status_first = True
    status = ib.adapter.place(buy(price=152))  # fills at once; status first
    assert (status.state, status.filled, status.avg_price) == (OrderState.FILLED, 1, D(152))


def test_no_broker_wide_cancel_when_the_fresh_listing_does_not_come(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)  # a duplicate copy: needs a global cancel
    ib.fake.mute_listing = True
    ib.adapter.cancel("p1-exit-1")
    assert ib.fake.global_cancels == 0
    assert any("could not be listed afresh" in a for a in ib.alarms)


def test_another_clients_order_with_our_reference_is_listed_as_foreign(ib: Ib) -> None:
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))
    ib.adapter.on_open_order(
        0, 8888, "p1-stop", 501, "NQ", "FOP", "SELL", 1, "LMT", D(300), None, None, "Submitted"
    )
    assert ib.adapter.status("p1-stop").state is OrderState.WORKING
    assert len(ib.fake.copies) == 1
    listed = sorted(o.order_id for o in ib.adapter.open_orders())
    assert listed == ["ib-8888", "p1-stop"]  # theirs is not a copy of ours
    assert not any("DUPLICATE" in a for a in ib.alarms)


def test_another_clients_reference_never_lends_its_id_to_our_order(ib: Ib) -> None:
    ib.adapter.on_open_order(
        0, 8889, "p9-exit-1", 501, "NQ", "FOP", "SELL", 1, "LMT", D(300), None, None, "Submitted"
    )
    ib.adapter.place(sell("p9-exit-1", price=300))
    assert ib.fake.sends[-1].ib_id == 100  # its own id from IBKR, never the other's 0
