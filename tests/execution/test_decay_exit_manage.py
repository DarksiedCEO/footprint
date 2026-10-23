"""Malik and Theta's decay exit (decision DX-1): the decay exit can only ADD a way out.

It is asked after every existing reason to get out (hard stop, gap, close, news, news
time stop, daily and weekly loss, kill switch), only on a fresh quote, only while no
exit is under way, with the open profit trade management itself uses (on the bid, net
of round-trip fees) and whether Booker's trail has locked. When it says out, the exit
is the existing one: the resting stop cancelled first, one limit sell at the bid in the
stop's one-cancels-all group, never a second seller. A decay exit that fails to answer
says nothing: the other exits run unchanged."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from footprint.agents.theta import DecayExit, DecayExitCheck
from footprint.core.models import Bar
from footprint.core.options import OptionChain
from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import ExitReason, Manager
from footprint.execution.orders import Order, OrderState, OrderType, Side
from tests.agents.test_decay_exit import expected
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import exit_rests_on_arrival, filled_position
from tests.execution.test_exit_guards import uncertain_on

D = Decimal
SEC = timedelta(seconds=1)
NOTE = "decay exit: modelled decay 99.00 over the next window, above the limit 12.50"


def verdict(fire: bool, reason: str = NOTE) -> DecayExitCheck:
    return DecayExitCheck(
        fire=fire,
        reason=reason,
        open_profit=D(25),
        limit_dollars=D("12.50"),
        decay_dollars=D(99),
        decay_points=4.95,
        implied_vol=0.2,
        years=0.01,
        underlying_price=D(20000),
    )


class FixedWatch:
    """A stand-in decay exit: always answers `fire` (or raises) and records each ask."""

    def __init__(self, fire: bool, raises: Exception | None = None) -> None:
        self.fire = fire
        self.raises = raises
        self.asked: list[tuple[str, int, Decimal, bool, datetime]] = []

    def check(
        self, contract_id: str, contracts: int, open_profit: Decimal, trailing: bool, now: datetime
    ) -> DecayExitCheck:
        self.asked.append((contract_id, contracts, open_profit, trailing, now))
        if self.raises is not None:
            raise self.raises
        return verdict(self.fire)


def _with(stack: Stack, watch: FixedWatch | DecayExit) -> None:
    stack.manager = Manager(
        stack.profiles,
        stack.manager._broker,
        stack.stops,
        stack.ledger,
        stack.halt,
        stack.book,
        stack.alarms.append,
        decay_exit=watch,
    )


def _sells(stack: Stack, position: Position) -> list[Order]:
    """Every limit sell (an exit) ever sent for the position's contract."""
    return [
        s.order
        for s in stack.broker._orders.values()
        if s.order.contract_id == position.contract_id
        and s.order.side is Side.SELL
        and s.order.order_type is OrderType.LIMIT
    ]


def _notes(position: Position) -> list[str]:
    return [e for e in position.events if "decay exit:" in e]


def test_a_decay_exit_closes_the_trade_at_the_bid_through_the_one_exit_path(
    stack: Stack,
) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)  # entry 151.5, stop 114
    stop_id = position.stop_order_id
    assert stop_id is not None
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(153, 154, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.DECAY_EXIT, D(153))
    assert closed.reason.value == "DECAY_EXIT"
    # Asked once, with trade management's own open profit: (153 - 151.5) x 20 - 5.
    assert watch.asked == [(position.contract_id, 1, D("25.0"), False, t)]
    sells = _sells(stack, position)
    assert len(sells) == 1  # one seller, never two
    exit_order = sells[0]
    assert (exit_order.limit_price, exit_order.quantity) == (D(153), position.contracts)
    assert exit_order.oca_group == position.position_id  # the stop's group
    assert stack.broker.status(stop_id).state is OrderState.CANCELLED  # stop down first
    assert stack.broker.positions() == {}
    assert _notes(position) == [f"{t.isoformat()} {NOTE}"]
    assert any(f"exit {ExitReason.DECAY_EXIT.value} at 153" in e for e in position.events)
    assert not stack.halt.tripped


def test_a_decay_exit_that_stays_changes_nothing(stack: Stack) -> None:
    watch = FixedWatch(False)
    _with(stack, watch)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(160, 161, t), (), t) is None
    assert len(watch.asked) == 1
    assert stack.stops.current(position.position_id).price == D(114)
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING
    assert _sells(stack, position) == []
    assert _notes(position) == []


def test_the_hard_stop_comes_before_the_decay_exit(stack: Stack) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    t = SEND + 10 * SEC
    closed = stack.step(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.STOP
    assert watch.asked == []


def test_every_existing_reason_comes_before_the_decay_exit(stack: Stack) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    stack.ledger.latch_kill_switch("test")
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.KILL_SWITCH
    assert watch.asked == []
    assert _notes(position) == []


def test_the_decay_exit_is_never_asked_on_a_stale_quote(stack: Stack) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    old = SEND + 30 * SEC
    stack.quote(160, 161, old)
    late = old + (stack.profiles.execution.quotes.max_age_seconds + 1) * SEC
    assert stack.manager.manage(position, (), late) is None
    assert watch.asked == []


def test_the_decay_exit_is_told_when_the_trail_has_locked(stack: Stack) -> None:
    watch = FixedWatch(False)
    _with(stack, watch)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    for k, bid in enumerate((210, 210, 195)):  # +1165 net twice (locks), then +865
        now = t + k * SEC
        assert stack.step(position, stack.quote(bid, bid + 1, now), (), now) is None
    assert [a[3] for a in watch.asked] == [False, False, True]
    assert watch.asked[-1][2] == D("865.0")
    assert stack.stops.current(position.position_id).trailing


def test_a_position_already_exiting_is_never_sold_twice(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)  # the decay exit's sell rests
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(153, 154, t), (), t) is None
    assert position.exit_order_id is not None
    assert len(watch.asked) == 1
    # The next quote works the one exit (re-priced to the bid); nothing else is sent
    # and the decay exit is not asked again.
    now = t + SEC
    closed = stack.step(position, stack.quote(D("152.75"), D("153.75"), now), (), now)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.DECAY_EXIT, D("152.75"))
    assert len(watch.asked) == 1
    sells = _sells(stack, position)
    assert len(sells) == 1
    assert stack.broker.positions() == {}
    assert _notes(position) == [f"{t.isoformat()} {NOTE}"]


def test_an_exit_begun_for_another_reason_is_not_asked_about_decay(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)
    stack.ledger.latch_kill_switch("test")
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(160, 161, t), (), t) is None
    assert position.exit_order_id is not None
    stack.step(position, stack.quote(159, 160, t + SEC), (), t + SEC)
    assert watch.asked == []
    assert len(_sells(stack, position)) == 1


def test_an_exit_that_must_wait_notes_the_decay_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    uncertain_on(stack, monkeypatch, 1)  # the first exit's positions read is uncertain
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(153, 154, t), (), t) is None
    assert _sells(stack, position) == []
    closed = stack.step(position, stack.quote(153, 154, t + SEC), (), t + SEC)
    assert closed is not None
    assert closed.reason is ExitReason.DECAY_EXIT
    assert len(watch.asked) == 2
    assert len(_notes(position)) == 1


def test_the_decay_exit_is_told_the_contracts_held(stack: Stack) -> None:
    # (max_contracts is 1 in v1; the hand-off must still carry the position's own size:
    # mutation survivor 31.)
    watch = FixedWatch(False)
    _with(stack, watch)
    position = filled_position(stack)
    position.contracts = 3
    t = SEND + 30 * SEC
    stack.step(position, stack.quote(160, 161, t), (), t)
    assert [a[1] for a in watch.asked] == [3]


def test_a_decay_exit_sent_again_after_its_order_died_notes_the_decay_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exit order dies at the broker; the same decay exit is sent again through the
    # same path. The journal's why is written once (mutation survivor 37).
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(153, 154, t), (), t) is None
    first = position.exit_order_id
    assert first is not None
    stack.broker.cancel(first)  # gone at the broker
    closed = stack.step(position, stack.quote(153, 154, t + SEC), (), t + SEC)
    assert closed is not None
    assert closed.reason is ExitReason.DECAY_EXIT
    assert len(_sells(stack, position)) == 2  # the dead one, then its one replacement
    assert len(_notes(position)) == 1
    assert len(watch.asked) == 1


def test_a_decay_exit_overtaken_by_another_reason_is_not_noted(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The decay exit said out but could not begin (positions uncertain); by the next
    # quote the kill switch has latched: that exit goes out as KILL_SWITCH and the
    # journal does not claim decay (mutation survivor 38).
    watch = FixedWatch(True)
    _with(stack, watch)
    position = filled_position(stack)
    uncertain_on(stack, monkeypatch, 1)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(153, 154, t), (), t) is None
    stack.ledger.latch_kill_switch("test")
    closed = stack.step(position, stack.quote(153, 154, t + SEC), (), t + SEC)
    assert closed is not None
    assert closed.reason is ExitReason.KILL_SWITCH
    assert _notes(position) == []


def test_a_decay_exit_that_fails_says_nothing_and_alarms_once(stack: Stack) -> None:
    watch = FixedWatch(True, raises=RuntimeError("chain feed broke"))
    _with(stack, watch)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    for k in range(2):
        now = t + k * SEC
        assert stack.step(position, stack.quote(153, 154, now), (), now) is None
    assert len(watch.asked) == 2
    failed = [a for a in stack.alarms if "decay exit" in a.message]
    assert len(failed) == 1
    assert "chain feed broke" in failed[0].message
    assert position.position_id in failed[0].message
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING
    assert _sells(stack, position) == []
    assert not stack.halt.tripped
    # The other exits still run: the hard stop.
    closed = stack.step(position, stack.quote(114, 116, t + 5 * SEC), (), t + 5 * SEC)
    assert closed is not None
    assert closed.reason is ExitReason.STOP


def test_with_no_decay_exit_the_manager_never_exits_for_decay(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(153, 154, t), (), t) is None
    assert _sells(stack, position) == []


# --- the real decay exit, on the stack's own option --------------------------------


def _real(stack: Stack, bars: list[Bar]) -> tuple[DecayExit, Decimal]:
    """Theta's decay exit on a chain of the stack's chosen option, at the last close;
    and that option's modelled decay over the window, for one contract, in dollars."""
    p = stack.profiles
    q = stack.approved.plan.choice.quote
    forward = bars[-1].close

    def chains(now: datetime) -> OptionChain:
        return OptionChain(p.instrument.symbol, now, forward, (q,))

    exit_ = DecayExit(p.strategy.theta, p.instrument, p.risk, p.execution.quotes, chains)
    theta = p.strategy.theta
    points = expected(q, SEND + 30 * SEC, theta, p.instrument, forward=forward)[1]
    return exit_, D(str(points)) * p.instrument.point_value


def _share(stack: Stack) -> Decimal:
    return stack.profiles.strategy.theta.decay_exit.max_pct_of_open_profit


def _open_profit(stack: Stack, position: Position, bid: Decimal) -> Decimal:
    p = stack.profiles
    fees = 2 * p.risk.fee_per_contract_per_side * position.contracts
    return (bid - position.entry_price) * p.instrument.point_value * position.contracts - fees


def test_theta_closes_a_small_winner_whose_decay_outruns_it(stack: Stack, bars: list[Bar]) -> None:
    exit_, decay = _real(stack, bars)
    _with(stack, exit_)
    position = filled_position(stack)
    bid = D(152)
    limit = _open_profit(stack, position, bid) * _share(stack) / 100
    assert D(0) < limit < decay
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(bid, bid + 1, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.DECAY_EXIT, bid)
    [note] = _notes(position)
    assert f"{decay:.2f}" in note
    assert f"{limit:.2f}" in note
    assert len(_sells(stack, position)) == 1


def test_theta_leaves_a_winner_that_outruns_its_decay(stack: Stack, bars: list[Bar]) -> None:
    exit_, decay = _real(stack, bars)
    _with(stack, exit_)
    position = filled_position(stack)
    bid = D(160)
    limit = _open_profit(stack, position, bid) * _share(stack) / 100
    assert limit > decay
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(bid, bid + 1, t), (), t) is None
    assert _sells(stack, position) == []


def test_theta_leaves_a_losing_trade_to_its_stop(stack: Stack, bars: list[Bar]) -> None:
    exit_, _ = _real(stack, bars)
    _with(stack, exit_)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    for k, bid in enumerate((D(151), D("151.75"), D(140))):  # -15, exactly 0, -235
        now = t + k * SEC
        assert stack.step(position, stack.quote(bid, bid + 1, now), (), now) is None
    assert _sells(stack, position) == []
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.WORKING
