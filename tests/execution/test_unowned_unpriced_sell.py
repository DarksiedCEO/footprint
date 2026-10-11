"""Grind review G4-1 (MEDIUM): a reconcile order that is not "the position's" and that
the broker reports FILLED with its price pending must never be sent again.

D39/D40 covered only a sell of a recorded position on its own contract. Two other
reconcile orders were left out:

- after a restart (`reconcile(None)`), the sell of what the broker still holds;
- with a position open, the sell of a stray holding of another contract.

Once such an order is FILLED it is no longer working, so nothing counted it, and while
the broker's report still showed the contract held (IBKR reports orderStatus Filled
before execDetails and the position report) reconcile sold again every settled round:
the reviewer's probes reached -5 and -4 at the simulator, that is, short.

Now any reconcile order this process sent for a contract, under any owner, that the
broker reports FILLED with its price pending holds that contract: nothing more is sent
for it, the hold is saved in the ledger (it survives a restart), and past
`fill_price_wait_seconds` the kill switch is latched with one CHECK THE FILL alarm.
The hold is released when the position it is saved under is booked (or by Andre's
`Ledger.book_by_hand`), never on the price alone while the report still lags."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.execution.executor.diego import Position
from footprint.execution.ledger import SoldHold
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Quote,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_check_the_fill_restart import _restart, _run
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag, _wait
from tests.execution.test_unpriced_sell_guards import LEFTOVER, _EndsWith, _filled_unpriced

D = Decimal
SEC = timedelta(seconds=1)


def _settle(stack: Stack) -> timedelta:
    return timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)


def _reconcile_sells(lag: Lag, contract_id: str) -> list[str]:
    return [o for o in lag.placed if o.startswith("reconcile-") and o.endswith(f"-{contract_id}")]


def _check_the_fill(alarms: list[str]) -> list[str]:
    return [a for a in alarms if "CHECK THE FILL" in a]


# --- P1: a restart, the position's own contract, no position record -------------


def test_after_a_restart_a_reconcile_sell_filled_before_its_price_is_never_sent_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reviewer's probe P1 at the runner level: the bot restarts with a position
    held, reconcile sells it, the sell fills with its price pending and the report
    still shows it held. One sell, never a second: the simulator is never short."""
    position = filled_position(stack)
    contract = position.contract_id
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")  # every reconcile sell of it: price pending
    lag.hold({contract: position.contracts})  # the report never catches up
    runner, ledger, halt, alarms = _restart(stack, tmp_path)
    start = SEND + 60 * SEC
    end = _run(stack, runner, start, 8 * (int(_settle(stack).total_seconds()) + 1) + 30)
    sent = _reconcile_sells(lag, contract)
    assert len(sent) == 1  # sold once; the lagging report is never sold again
    assert SimulatedBroker.positions(stack.broker) == {}  # never short at the broker
    assert ledger.sold_unpriced == (SoldHold(position.position_id, contract, sent[0]),)
    assert end - start > _wait(stack)  # past the wait: latched, Andre told once
    assert ledger.account(end).kill_switch_latched
    assert halt.tripped
    assert [r for r in halt.reasons if sent[0] in r]  # the halt names the filled order
    told = _check_the_fill([a.message for a in alarms])
    assert len(told) == 1
    assert sent[0] in told[0]
    assert "flatten by hand" in told[0]
    assert "book_by_hand" in told[0]
    assert position.position_id in told[0]  # the id to book it under


def test_after_a_restart_inside_the_wait_the_hold_is_kept_without_the_alarm(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inside `fill_price_wait_seconds` the hold is quiet: no second sell, no CHECK THE
    FILL yet (reconcile's own mismatch halt still stands)."""
    position = filled_position(stack)
    contract = position.contract_id
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    runner, ledger, _, alarms = _restart(stack, tmp_path)
    start = SEND + 60 * SEC
    t = start
    sold_at: datetime | None = None
    for second in range(120):
        t = start + second * SEC
        stack.quote(150, 151, t)
        runner.step(t)
        if sold_at is None and _reconcile_sells(lag, contract):
            sold_at = t
        if sold_at is not None and t - sold_at >= _wait(stack) - SEC:
            break
    assert sold_at is not None
    assert len(_reconcile_sells(lag, contract)) == 1
    assert not _check_the_fill([a.message for a in alarms])
    assert ledger.sold_unpriced  # saved from the moment it is known


def test_the_hold_survives_a_second_restart(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restarted process's own reconcile sell is held durably once it has seen it
    fill: a second restart inside the wait does not sell beside it either. (A process
    that dies before it sees its own order fill keeps no record of it; that gap is
    the same for every order sent just before a crash and is not covered here.)"""
    position = filled_position(stack)
    contract = position.contract_id
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    runner, first, _, _ = _restart(stack, tmp_path)
    start = SEND + 60 * SEC
    t = start
    while not first.sold_unpriced:  # the first process saw its sell fill, unpriced
        t += SEC
        assert t - start < 60 * SEC
        stack.quote(150, 151, t)
        runner.step(t)
    again, _, _, alarms = _restart(stack, tmp_path)
    _run(stack, again, t + SEC, 30)
    assert len(_reconcile_sells(lag, contract)) == 1
    assert SimulatedBroker.positions(stack.broker) == {}
    assert len(_check_the_fill([a.message for a in alarms])) == 1


def test_with_no_position_in_the_ledger_the_hold_is_kept_under_the_order(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ledger never recorded the position (its write failed at the fill): the hold
    is saved under the reconcile order's own id, Andre is told to book it under that
    id, and booking it by hand releases it."""
    position = filled_position(stack)
    contract = position.contract_id
    stack.ledger._save(replace(stack.ledger._state, open_position_id=None))
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    runner, ledger, _, alarms = _restart(stack, tmp_path)
    start = SEND + 60 * SEC
    end = _run(stack, runner, start, 60)
    sent = _reconcile_sells(lag, contract)
    assert len(sent) == 1
    assert ledger.sold_unpriced == (SoldHold(sent[0], contract, sent[0]),)
    told = _check_the_fill([a.message for a in alarms])
    assert len(told) == 1
    assert f"book_by_hand as {sent[0]}" in told[0]
    ledger.book_by_hand(end, sent[0], D(0), "Andre")
    assert not ledger.sold_unpriced


# --- P2: a stray holding of another contract while the position is open ---------


def _stray_rounds(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, rounds: int, start: datetime
) -> tuple[Lag, Position, datetime]:
    """Reconcile with the position open and a stray holding the report never drops."""
    position = filled_position(stack)
    sim = stack.broker
    sim._positions[LEFTOVER] = 1
    lag = Lag(sim, monkeypatch)
    lag.hidden = _EndsWith(f"-{LEFTOVER}")
    monkeypatch.setattr(sim, "positions", lambda: {**SimulatedBroker.positions(sim), LEFTOVER: 1})
    now = start
    for k in range(rounds):
        now = start + k * (_settle(stack) + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(LEFTOVER, D(40), D(41), now))
        stack.manager.reconcile(position, now)
    return lag, position, now


def test_a_stray_sell_filled_before_its_price_is_never_sent_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe P2: the stray is sold once, its sell fills with the price
    pending, the report still shows it held. Never sold again; the position's own
    contract is still flattened."""
    start = SEND + 60 * SEC
    lag, _, end = _stray_rounds(stack, monkeypatch, 10, start)
    stray = _reconcile_sells(lag, LEFTOVER)
    assert len(stray) == 1
    assert SimulatedBroker.positions(stack.broker).get(LEFTOVER, 0) == 0  # never short
    own = [o for o in lag.placed if o.startswith("reconcile-") and LEFTOVER not in o]
    assert own  # the position's own contract is sold as well
    assert stack.broker.positions().get(stack.contract, 0) == 0
    pid = stack.ledger.open_position_id
    assert pid is not None
    assert stack.ledger.sold_unpriced == (SoldHold(pid, LEFTOVER, stray[0]),)
    assert end - start > _wait(stack)
    assert stack.ledger.account(end).kill_switch_latched
    told = _check_the_fill([a.message for a in stack.alarms])
    assert len(told) == 1
    assert stray[0] in told[0]
    assert LEFTOVER in told[0]


def test_a_stray_hold_is_quiet_inside_the_wait(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    rounds = 4
    assert rounds * (_settle(stack) + SEC) < _wait(stack)
    lag, _, _ = _stray_rounds(stack, monkeypatch, rounds, SEND + 60 * SEC)
    assert len(_reconcile_sells(lag, LEFTOVER)) == 1
    assert not _check_the_fill([a.message for a in stack.alarms])


def test_a_stray_hold_is_kept_when_its_price_arrives_and_the_report_still_lags(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The price comes but the report still shows the stray held: that report is not
    acted on (fail closed; a second sell could go short). The saved hold stays until
    the position is booked."""
    start = SEND + 60 * SEC
    lag, position, end = _stray_rounds(stack, monkeypatch, 6, start)
    assert stack.ledger.sold_unpriced
    lag.hidden = set()  # every price arrives; the stray is still reported held
    for k in range(1, 6):
        now = end + k * (_settle(stack) + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(LEFTOVER, D(40), D(41), now))
        stack.manager.reconcile(position, now)
    assert len(_reconcile_sells(lag, LEFTOVER)) == 1
    assert SimulatedBroker.positions(stack.broker).get(LEFTOVER, 0) == 0
    assert [h for h in stack.ledger.sold_unpriced if h.contract_id == LEFTOVER]


def test_a_reconcile_buy_filled_before_its_price_is_never_sent_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for a buy back of a short holding: bought once, never again while the
    report lags (a second buy would leave a long nobody asked for)."""
    position = filled_position(stack)
    sim = stack.broker
    sim._positions[LEFTOVER] = -1
    lag = Lag(sim, monkeypatch)
    lag.hidden = _EndsWith(f"-{LEFTOVER}")

    monkeypatch.setattr(sim, "positions", lambda: {**SimulatedBroker.positions(sim), LEFTOVER: -1})
    start = SEND + 60 * SEC
    for k in range(10):
        now = start + k * (_settle(stack) + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(LEFTOVER, D(40), D(41), now))
        stack.manager.reconcile(position, now)
    assert len(_reconcile_sells(lag, LEFTOVER)) == 1
    assert SimulatedBroker.positions(sim).get(LEFTOVER, 0) == 0  # never long either


def test_a_restart_sell_seen_again_under_the_position_waits_quietly(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile ran without the position record (`reconcile(None)`) and sold; the
    sell filled with its price pending. Reconcile then runs with the position record:
    that sell is still known as this process's own, so the hold waits out the price
    quietly instead of raising the "before this restart" alarm at once."""
    position = filled_position(stack)
    contract = position.contract_id
    lag = Lag(stack.broker, monkeypatch)
    lag.hidden = _EndsWith(f"-{contract}")
    lag.hold({contract: position.contracts})
    start = SEND + 60 * SEC
    now = start
    while not stack.ledger.sold_unpriced:
        now += _settle(stack) + SEC
        assert now - start < _wait(stack)
        stack.quote(150, 151, now)
        stack.manager.reconcile(None, now)
    for _ in range(2):
        now += SEC
        assert now - start < _wait(stack)
        stack.quote(150, 151, now)
        stack.manager.reconcile(position, now)
    assert len(_reconcile_sells(lag, contract)) == 1
    assert not _check_the_fill([a.message for a in stack.alarms])


def test_every_reconcile_order_is_searched_for_the_unpriced_fill(stack: Stack) -> None:
    """Under one owner the list mixes contracts, and an order whose placing failed was
    never placed (the broker does not know it): neither stops the search for an older
    FILLED-unpriced order of the contract asked about."""
    sold = "reconcile-1-x-" + LEFTOVER
    _filled_unpriced(stack, sold, LEFTOVER, Side.SELL)
    other = Order("reconcile-2-x-OTHER", "OTHER", Side.SELL, 1, OrderType.LIMIT, D(5))
    stack.broker._orders[other.order_id] = OrderStatus(other, OrderState.WORKING, 0, None)
    stack.manager._reconcile_orders[None] = [sold, "reconcile-3-x-never-placed", other.order_id]
    assert stack.manager._unpriced_reconcile_order(LEFTOVER) == sold
    assert stack.manager._unpriced_reconcile_order("OTHER") is None
