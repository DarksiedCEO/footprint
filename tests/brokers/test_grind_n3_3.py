"""Delta review 3 of 9e22f61, N3-3 (claude/night-review-9e22f61.md).

The N-3 fix re-arms an Inactive order's cancel when ibapi answers it 504 ("Not
connected"). But the binding drops every error once its session is marked closed
(`IbWrapper.error` while `_open` is False), and `_after_event` sends its cancels outside
the lock. A cancel decided while connected and sent just after the session closed is
then answered 504 into nothing: `cancel_sent` stays True, and the cancel was never sent
again (the F-3 hole, for that window).

Fix: once IBKR is back and resynced, an order IBKR still lists as Inactive whose cancel
it never answered is cancelled again. It does not matter whether the 504 arrived, or
whether ibapi reports it with reqId = orderId (UNVERIFIED). A cancel sent twice is
harmless; one never sent leaves an order that could become active again. Until it is
ready, the mark stays, so an order that comes back acting meanwhile is still said to be
LIVE again (and reconciled)."""

from decimal import Decimal

import pytest

from footprint.execution.orders import Order, OrderType, Side
from tests.brokers.test_ibkr_adapter import C, Ib, make

D = Decimal


@pytest.fixture
def ib() -> Ib:
    return make()


def _stop(ib: Ib) -> tuple[int, int]:
    ib.adapter.place(Order("p1-stop", C, Side.SELL, 1, OrderType.LIMIT, D(300), oca_group="p1"))
    stop = next(c for c in ib.fake.copies.values() if c.request.order_ref == "p1-stop")
    return stop.ib_id, stop.perm_id


def _cancel_lost_unanswered(ib: Ib, ib_id: int, perm: int) -> list[int]:
    """The order turns Inactive; its cancel is sent just as the session closes, and the
    504 the library answers is dropped by the closed binding: nothing comes back."""
    real_cancel = ib.fake.cancel_order
    tried: list[int] = []

    def dropped(ib_id: int) -> None:
        tried.append(ib_id)  # no raise, no error() reaching the adapter

    ib.fake.cancel_order = dropped  # type: ignore[method-assign]
    ib.fake.inactive.add(perm)
    ib.adapter.on_order_status(ib_id, perm, "Inactive", 0, 1, D(0))
    ib.fake.cancel_order = real_cancel  # type: ignore[method-assign]
    assert tried == [ib_id]
    assert ib.adapter.status("p1-stop").held_back  # no protection meanwhile
    return tried


def test_a_cancel_whose_504_was_dropped_is_sent_again_once_ready(ib: Ib) -> None:
    ib_id, perm = _stop(ib)
    _cancel_lost_unanswered(ib, ib_id, perm)
    ib.fake.lose()
    assert ib.fake.cancels.count(ib_id) == 0  # nothing is sent while IBKR is away
    ib.fake.restore(data_lost=True)  # the resync lists it Inactive still
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 1  # sent again, once
    assert not ib.adapter.status("p1-stop").protects


def test_the_cancel_is_not_sent_again_before_ibkr_is_ready(ib: Ib) -> None:
    """D35: back but not resynced, nothing is sent; the cancel waits for ready."""
    ib_id, perm = _stop(ib)
    _cancel_lost_unanswered(ib, ib_id, perm)
    ib.fake.lose()
    ib.fake.defer_executions = True  # the resync cannot finish yet
    ib.fake.restore(data_lost=True)
    assert not ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 0
    ib.fake.flush_executions()
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 1
    assert not ib.adapter.status("p1-stop").protects


def test_an_order_back_acting_after_the_loss_is_still_said_live_again(ib: Ib) -> None:
    """The mark is not cleared on the loss: if IBKR lists the order acting again when it
    is back, it is still LIVE again after it was Inactive (reconciled), never cancelled
    blind and never taken as protection silently."""
    ib_id, perm = _stop(ib)
    _cancel_lost_unanswered(ib, ib_id, perm)
    ib.fake.lose()
    ib.fake.inactive.discard(perm)  # IBKR lists it Submitted on the resync
    ib.alarms.clear()
    ib.fake.restore(data_lost=True)
    assert any("LIVE again at IBKR after it was Inactive" in a for a in ib.alarms), ib.alarms
    assert ib.fake.cancels.count(ib_id) == 0


def test_a_cancel_ibkr_answered_is_not_sent_again_after_the_loss(ib: Ib) -> None:
    """A cancel IBKR already answered (cancelled / not found) is settled: a reconnect
    does not send it again, even if the order is listed Inactive once more."""
    ib_id, perm = _stop(ib)
    ib.fake.inactive.add(perm)
    ib.fake.mute_cancels = True  # the cancel is sent, its answer comes below
    ib.adapter.on_order_status(ib_id, perm, "Inactive", 0, 1, D(0))
    ib.adapter.on_error(ib_id, 202, "Order Canceled")  # IBKR answered the cancel
    sent = ib.fake.cancels.count(ib_id)
    ib.fake.lose()
    ib.fake.restore(data_lost=True)
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == sent  # not sent again


def test_a_cancel_that_reached_ibkr_is_not_sent_again(ib: Ib) -> None:
    """The cancel did reach IBKR: the order is no longer listed when IBKR is back, so it
    is settled from the listing and nothing is sent again."""
    ib_id, perm = _stop(ib)
    _cancel_lost_unanswered(ib, ib_id, perm)
    ib.fake.lose()
    ib.fake.inactive.discard(perm)
    copy = next(c for c in ib.fake.copies.values() if c.perm_id == perm)
    ib.fake.sim.cancel(copy.sim_id)  # IBKR did cancel it; the answer was lost
    ib.fake.restore(data_lost=True)
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 0
    assert not ib.adapter.status("p1-stop").protects


def test_a_cancel_pending_at_ibkr_keeps_its_mark_across_the_loss(ib: Ib) -> None:
    """Only an order still Inactive is cancelled again. One IBKR lists PendingCancel
    (our cancel reached it) keeps the mark, so if IBKR then refuses the cancel and the
    order acts again, it is still said to be LIVE again after it was Inactive."""
    ib_id, perm = _stop(ib)
    ib.fake.slow_cancels = True  # IBKR answers PendingCancel and holds it there
    ib.fake.inactive.add(perm)
    ib.adapter.on_order_status(ib_id, perm, "Inactive", 0, 1, D(0))
    assert ib.fake.cancels.count(ib_id) == 1
    ib.fake.lose()
    ib.fake.restore(data_lost=True)  # listed PendingCancel on the resync
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 1  # not cancelled again
    ib.alarms.clear()
    ib.adapter.on_order_status(ib_id, perm, "Submitted", 0, 1, D(0))  # cancel refused
    assert any("LIVE again at IBKR after it was Inactive" in a for a in ib.alarms), ib.alarms


def test_an_order_first_seen_inactive_during_the_resync_waits_for_ready(ib: Ib) -> None:
    """D35 (reviewer-style mutant N6): back but still resyncing is not ready. An order
    the resync lists Inactive is not cancelled until the resync is done."""
    ib_id, perm = _stop(ib)
    ib.fake.lose()
    ib.fake.inactive.add(perm)  # IBKR made it Inactive while we were away
    ib.fake.defer_executions = True  # the resync cannot finish yet
    ib.fake.restore(data_lost=True)
    assert not ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 0  # nothing sent while resyncing
    ib.fake.flush_executions()
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 1
    assert not ib.adapter.status("p1-stop").protects
