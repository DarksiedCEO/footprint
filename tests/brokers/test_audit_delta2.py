"""Delta review 2 of 1cd84c8 (claude/audit-2026-10-07-delta2.md): N-3 and the
surviving mutants R3, R5 and R9.

N-3: ibapi does not raise when a cancel cannot be sent because the socket is gone; it
answers through `error()` with 504 ("Not connected"). That answer re-arms an Inactive
order's cancel exactly as a raised send does (F-3): sent again once IBKR is ready."""

from decimal import Decimal

import pytest

from footprint.execution.orders import Order, OrderType, Side
from tests.brokers.test_audit_delta import _extra_copy, _resting
from tests.brokers.test_ibkr_adapter import C, Ib, make

D = Decimal


@pytest.fixture
def ib() -> Ib:
    return make()


def _stop(ib: Ib) -> tuple[int, int]:
    ib.adapter.place(Order("p1-stop", C, Side.SELL, 1, OrderType.LIMIT, D(300), oca_group="p1"))
    stop = next(c for c in ib.fake.copies.values() if c.request.order_ref == "p1-stop")
    return stop.ib_id, stop.perm_id


def test_a_cancel_answered_not_connected_is_sent_again_once_ready(ib: Ib) -> None:
    """N-3: the send does not raise; IBKR's library answers 504 through error()."""
    ib_id, perm = _stop(ib)
    real_cancel = ib.fake.cancel_order
    tried: list[int] = []

    def not_connected(ib_id: int) -> None:
        tried.append(ib_id)
        assert ib.fake.events is not None
        ib.fake.events.on_error(ib_id, 504, "Not connected")  # as ibapi does

    ib.fake.cancel_order = not_connected  # type: ignore[method-assign]
    ib.resyncs.clear()
    ib.fake.inactive.add(perm)
    ib.adapter.on_order_status(ib_id, perm, "Inactive", 0, 1, D(0))
    assert tried == [ib_id]
    assert ib.resyncs  # what IBKR holds is in doubt: reconcile
    assert ib.adapter.status("p1-stop").held_back  # still no protection meanwhile
    ib.fake.cancel_order = real_cancel  # type: ignore[method-assign]
    ib.fake.lose()
    ib.fake.restore(data_lost=True)  # the resync replays it Inactive
    assert ib.adapter.ready
    assert ib.fake.cancels.count(ib_id) == 1  # sent again, once


def test_a_not_connected_answer_to_anything_else_changes_nothing(ib: Ib) -> None:
    """504 on an order that is not an Inactive one being cancelled is only recorded."""
    ib_id, _ = _stop(ib)
    ib.resyncs.clear()
    ib.adapter.on_error(ib_id, 504, "Not connected")
    assert not ib.resyncs
    assert ib.adapter.status("p1-stop").protects


def test_any_error_raised_by_a_cancel_send_re_arms_it(ib: Ib) -> None:
    """R5: not only ConnectionError: whatever the send raises, the cancel is not lost."""
    ib_id, perm = _stop(ib)
    real_cancel = ib.fake.cancel_order

    def broken(ib_id: int) -> None:
        raise RuntimeError("binding failure")

    ib.fake.cancel_order = broken  # type: ignore[method-assign]
    ib.resyncs.clear()
    ib.fake.inactive.add(perm)
    ib.adapter.on_order_status(ib_id, perm, "Inactive", 0, 1, D(0))
    assert ib.resyncs
    ib.fake.cancel_order = real_cancel  # type: ignore[method-assign]
    ib.fake.lose()
    ib.fake.restore(data_lost=True)
    assert ib.fake.cancels.count(ib_id) == 1


def test_one_execution_at_zero_among_real_ones_is_no_price(ib: Ib) -> None:
    """R9: a fill priced from 0 and 146 would book 73, a guessed price."""
    ib_id, perm = _resting(ib, quantity=2)
    for n, price in ((11, "0"), (12, "146")):
        ib.adapter.on_execution(
            f"0000f{n:04x}.65a1b2c3.01.01", ib_id, perm, 501, "NQ", "FOP", "BOT", 1, D(price)
        )
    ib.adapter.on_order_status(ib_id, perm, "Filled", 2, 0, D(0))
    assert ib.adapter.status("p1-entry").avg_price is None


def test_a_failed_cancel_re_arms_only_the_inactive_copy(ib: Ib) -> None:
    """R3: a copy already being cancelled (cancelled by us after it was Inactive) keeps
    that mark when another copy's cancel fails, so if it acts again it is still said
    to be LIVE again (and reconciled)."""
    listed, ib_id, second = _extra_copy(ib)
    first = min(c.perm_id for c in ib.fake.copies.values() if c.request.order_ref == "p1-exit-1")
    assert first != second
    ib.adapter.on_order_status(ib_id, first, "Inactive", 0, 1, D(0))  # cancelled by us
    ib.adapter.on_order_status(ib_id, first, "PendingCancel", 0, 1, D(0))
    real_cancel = ib.fake.cancel_order

    def dropped(ib_id: int) -> None:
        raise ConnectionError("socket closed")

    ib.fake.cancel_order = dropped  # type: ignore[method-assign]
    ib.adapter.on_order_status(ib_id, second, "Inactive", 0, 1, D(0))  # its cancel fails
    ib.fake.cancel_order = real_cancel  # type: ignore[method-assign]
    ib.alarms.clear()
    ib.adapter.on_order_status(ib_id, first, "Submitted", 0, 1, D(0))  # acting again
    assert any("LIVE again at IBKR after it was Inactive" in a for a in ib.alarms), ib.alarms
    assert listed.endswith(f"#{second}")
