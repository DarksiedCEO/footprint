"""Independent audit 2026-10-07 (claude/audit-2026-10-07.md): findings A-1, A-2, A-3.

Each test failed on e3a21cf and pins the fix.
"""

from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.calendar import entry_cutoff
from footprint.core.models import Bar
from footprint.execution.orders import OrderState
from tests.brokers.test_ibkr_adapter import Ib, buy, make, sell
from tests.brokers.test_ibkr_runner import open_through_ibkr, through_ibkr
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)


@pytest.fixture
def ib() -> Ib:
    return make()


# --- A-1: a duplicate live copy sends the runner to reconcile -----------------------------


def test_a_duplicate_live_copy_raises_a_doubt(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", group="p1"))
    ib.resyncs.clear()
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(199), None)  # IBKR keeps the first, adds a copy
    assert any("DUPLICATE LIVE ORDER" in a for a in ib.alarms)
    assert ib.resyncs  # the alarm says "reconcile": the runner must be told


def test_duplicate_entry_copies_never_hold_more_than_one_contract(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter = through_ibkr(stack, [])
    runner, _, _ = runner_for(stack, bars, tmp_path)
    adapter._on_resync = runner.doubt  # live wiring
    runner.step(DECISION_TIME)  # the entry goes out (with its stop attached)
    fake.degraded = True  # from now on every modify adds a live copy at IBKR
    held_max = 0
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
        held_max = max(held_max, *adapter.positions().values(), 0)
    t = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), t)  # every live entry copy would fill here
    runner.step(t)
    held_max = max(held_max, *adapter.positions().values(), 0)
    assert held_max <= 1
    live_entries = [
        c
        for c in fake.copies.values()
        if c.request.order_ref.endswith("-entry")
        and fake.sim.status(c.sim_id).state is OrderState.WORKING
    ]
    assert len(live_entries) <= 1


# --- A-2: a fill reported at price 0 is never booked at 0 ---------------------------------


def _copy(ib: Ib, ref: str):  # type: ignore[no-untyped-def]
    return next(c for c in ib.fake.copies.values() if c.request.order_ref == ref)


def test_a_fill_told_at_price_zero_keeps_the_executed_price(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # fills at the ask (152), execution told first
    entry = _copy(ib, "p1-entry")
    ib.adapter.on_order_status(entry.ib_id, entry.perm_id, "Filled", 1, 0, D(0))
    status = ib.adapter.status("p1-entry")
    assert status.state is OrderState.FILLED
    assert status.avg_price == D(152)


def test_a_fill_told_at_price_zero_with_no_execution_has_no_price(ib: Ib) -> None:
    ib.adapter.place(buy(price=140))  # rests
    entry = _copy(ib, "p1-entry")
    ib.adapter.on_order_status(entry.ib_id, entry.perm_id, "Filled", 1, 0, D(0))
    status = ib.adapter.status("p1-entry")
    assert status.filled == 1
    assert status.avg_price is None  # unknown until the execution: never 0


def test_an_average_is_never_taken_from_only_some_of_the_filled_copies(ib: Ib) -> None:
    ib.adapter.place(sell("p1-exit-1", price=300, group="p1"))  # rests
    ib.fake.degraded = True
    ib.adapter.modify("p1-exit-1", D(299), None)  # a second live copy
    first, second = sorted(ib.fake.copies.values(), key=lambda c: c.perm_id)
    ib.adapter.on_order_status(first.ib_id, first.perm_id, "Filled", 1, 0, D(300))
    ib.adapter.on_order_status(second.ib_id, second.perm_id, "Filled", 1, 0, D(0))
    status = ib.adapter.status("p1-exit-1")
    assert status.filled == 2
    assert status.avg_price is None  # one fill's price is unknown: no average yet


# --- A-3: nothing is sent while IBKR is away, not even an Inactive order's cancel ---------


def test_an_inactive_report_while_disconnected_sends_no_cancel(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))  # long 1
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))  # rests
    stop = _copy(ib, "p1-stop")
    ib.fake.lose()
    before = list(ib.fake.cancels)
    ib.resyncs.clear()
    ib.adapter.on_order_status(stop.ib_id, stop.perm_id, "Inactive", 0, 1, D(0))
    assert ib.fake.cancels == before  # D35(2): nothing is sent while disconnected
    assert ib.resyncs  # held back: reconcile once IBKR is back


def test_an_inactive_report_before_the_first_resync_sends_no_cancel() -> None:
    ib = make(connect=False)
    ib.adapter.on_open_order(
        7, 2001, "p1-stop", 501, "NQ", "FOP", "SELL", 1, "STP LMT", D(99), D(100), "p1",
        "Inactive",
    )  # fmt: skip
    assert ib.fake.cancels == []
    assert ib.fake.global_cancels == 0
    assert ib.resyncs  # held back, not protection: reconcile once IBKR is ready


def test_an_inactive_order_seen_while_away_is_cancelled_once_ready(ib: Ib) -> None:
    ib.adapter.place(buy(price=152))
    ib.adapter.place(sell("p1-stop", price=300, group="p1"))
    stop = _copy(ib, "p1-stop")
    ib.fake.lose()
    ib.fake.inactive.add(stop.perm_id)  # IBKR holds it Inactive across the outage
    ib.adapter.on_order_status(stop.ib_id, stop.perm_id, "Inactive", 0, 1, D(0))
    assert stop.ib_id not in ib.fake.cancels
    ib.fake.restore(data_lost=True)  # resync replays it Inactive; now it may be sent
    assert ib.adapter.ready
    assert ib.fake.cancels.count(stop.ib_id) == 1
    status = ib.adapter.status("p1-stop")
    assert status.held_back or status.state is not OrderState.WORKING


def test_an_inactive_report_while_ready_is_still_cancelled_at_once(ib: Ib) -> None:
    ib.adapter.place(buy(price=140))  # rests
    entry = _copy(ib, "p1-entry")
    ib.fake.slow_cancels = True
    ib.fake.inactivate(entry.perm_id)
    assert ib.fake.cancels == [entry.ib_id]


def test_the_runner_never_cancels_through_the_adapter_while_away(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    alarms: list[str] = []
    fake, adapter, runner, fill = open_through_ibkr(stack, bars, tmp_path, alarms)
    stop = next(c for c in fake.copies.values() if c.request.order_ref.endswith("-stop"))
    fake.lose()
    before = list(fake.cancels)
    adapter.on_order_status(stop.ib_id, stop.perm_id, "Inactive", 0, 1, D(0))
    runner.step(fill + SEC)
    assert fake.cancels == before
    assert runner.reconciling


# --- A-6: losing IBKR in the middle of an exit is an outage, not a halt (D35) -------------


def test_losing_ibkr_mid_exit_waits_without_a_halt_or_a_latch(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    fake, adapter, runner, fill = open_through_ibkr(stack, bars, tmp_path, [])
    position = runner.position
    assert position is not None
    real_cancel = fake.cancel_order

    def cancel_then_drop(ib_id: int) -> None:
        real_cancel(ib_id)
        fake.lose()  # Gateway goes away right after the stop's cancel is sent

    fake.cancel_order = cancel_then_drop  # type: ignore[method-assign]
    lead = stack.profiles.strategy.console.close_lead_minutes
    t = entry_cutoff(fill, stack.profiles.instrument, lead).astimezone(UTC) + SEC
    stack.quote(160, 161, t)  # at the close: the exit begins with the stop's cancel
    runner.step(t)
    assert fake.cancels  # the exit had begun
    assert not stack.halt.tripped  # D35: a lost broker is not a halt
    assert not stack.ledger.account(t).kill_switch_latched
    assert runner.reconciling
    fake.cancel_order = real_cancel  # type: ignore[method-assign]
    fake.restore(data_lost=True)
    for k in range(1, 15):
        now = t + k * SEC
        stack.quote(160, 161, now)
        runner.step(now)
    assert adapter.positions() == {}  # out once IBKR is back, never short
    assert runner.position is None
    assert not stack.halt.tripped
