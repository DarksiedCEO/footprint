"""Q5.5-b LATCHED (HARDENING H1-0), inside trade management (tests/execution stack).

APPROVED BY ANDRE 2026-10-11 10:30 PT: once the position's open profit reaches the
target, the target verdict is held for that position until it is gone. If the exit
cannot begin yet (positions uncertain, or a refused stop cancel) it waits exactly as
before; it then closes as soon as an exit can begin, at the bid then, even if the bid
has fallen back below the target. Limit at the bid, stop cancelled first, never sell
twice, hard stop unchanged and asked first. Backtest-only (TargetExit).

Fixture arithmetic (as tests/execution/test_exit_target_rule.py): entry 151.5, point
value 20, one contract, fees 2.5 a side, so the net open profit on a bid b is
(b - 151.5) x 20 - 5. A bid of 201.75 is +1000.00 exactly; 180 is +565.00. Synthetic
quotes say NOTHING about profitability.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from footprint.backtest.exits import TargetExit
from footprint.execution.management.malik import ExitReason
from footprint.execution.orders import Order, OrderState, OrderStatus, Side
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_exit_guards import uncertain_on
from tests.execution.test_exit_target_rule import AT_TARGET, _with_rule

D = Decimal
SEC = timedelta(seconds=1)
FELL_BACK = D(180)  # net +565.00, well below the target


def _sells(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> list[Order]:
    """Every SELL the manager sends from here on (through the order record)."""
    sent: list[Order] = []
    real = stack.broker.place

    def place(order: Order) -> OrderStatus:
        if order.side is Side.SELL:
            sent.append(order)
        return real(order)

    monkeypatch.setattr(stack.broker, "place", place)
    return sent


def test_target_reached_while_positions_uncertain_closes_later_below_the_target(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    sells = _sells(stack, monkeypatch)
    uncertain_on(stack, monkeypatch, 1)  # the first exit's positions read is in doubt
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t) is None
    stop = stack.broker.status(f"{position.position_id}-stop")
    assert stop.state is OrderState.WORKING  # the exit waited; the stop still protects
    assert position.exit_order_id is None
    assert sells == []
    assert not stack.stops.current(position.position_id).trailing

    t = SEND + 31 * SEC
    closed = stack.step(position, stack.quote(FELL_BACK, FELL_BACK + 1, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, FELL_BACK)
    assert closed.net_pnl == D("565.00")  # the whole position, at the bid then
    assert stack.broker.positions() == {}
    assert len(sells) == 1  # sold once
    assert sells[0].limit_price == FELL_BACK  # a limit at the bid then


def test_target_reached_while_the_stop_cancel_is_refused_closes_later_below_the_target(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    sells = _sells(stack, monkeypatch)
    stop_id = f"{position.position_id}-stop"
    real_cancel = stack.broker.cancel
    refusals = {"left": 1}

    def cancel(order_id: str) -> OrderStatus:
        if refusals["left"]:
            refusals["left"] -= 1
            return stack.broker.status(order_id)  # refused: IBKR never confirms it
        return real_cancel(order_id)

    monkeypatch.setattr(stack.broker, "cancel", cancel)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t) is None
    assert stack.broker.status(stop_id).state is OrderState.WORKING  # still protecting
    assert position.exit_order_id is None  # never an exit beside a live stop
    assert sells == []
    assert not stack.stops.current(position.position_id).trailing

    t = SEND + 31 * SEC
    closed = stack.step(position, stack.quote(FELL_BACK, FELL_BACK + 1, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, FELL_BACK)
    assert stack.broker.status(stop_id).state is OrderState.CANCELLED  # stop first
    assert stack.broker.positions() == {}
    assert len(sells) == 1
    assert sells[0].limit_price == FELL_BACK


def test_while_latched_and_waiting_the_trail_never_engages(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two quotes at the target engage the baseline trail (test_exit_target_rule); with
    the latch held and the exit waiting on both, the trail is never moved."""
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    uncertain_on(stack, monkeypatch, 1, 2, 3)
    for k, bid in ((30, AT_TARGET), (31, AT_TARGET), (32, FELL_BACK)):
        t = SEND + k * SEC
        assert stack.step(position, stack.quote(bid, bid + 1, t), (), t) is None
    assert not stack.stops.current(position.position_id).trailing
    assert stack.stops.current(position.position_id).price == D(114)  # hard stop as was


def test_while_latched_the_hard_stop_still_acts_first(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    uncertain_on(stack, monkeypatch, 1)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t) is None
    t = SEND + 31 * SEC
    closed = stack.step(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(114))


def _pos(pid: str) -> SimpleNamespace:
    return SimpleNamespace(position_id=pid)


def test_the_verdict_is_latched_per_position() -> None:
    rule = TargetExit(D(1000))
    a, b = _pos("a"), _pos("b")
    assert not rule.wants_out(a, D("999.99"), SEND)  # type: ignore[arg-type]
    assert rule.wants_out(a, D("1000.00"), SEND)  # type: ignore[arg-type]
    assert rule.wants_out(a, D("999.99"), SEND + SEC)  # type: ignore[arg-type]  # held
    assert rule.wants_out(a, D(-750), SEND + 2 * SEC)  # type: ignore[arg-type]  # held
    # another position (a new one after `a` is gone) starts unlatched
    assert not rule.wants_out(b, D("999.99"), SEND + 3 * SEC)  # type: ignore[arg-type]
    assert rule.wants_out(b, D("1000.00"), SEND + 4 * SEC)  # type: ignore[arg-type]
    assert rule.wants_out(b, D(0), SEND + 5 * SEC)  # type: ignore[arg-type]


def test_below_the_target_nothing_is_latched() -> None:
    rule = TargetExit(D(1000))
    a = _pos("a")
    for k in range(3):
        assert not rule.wants_out(a, D("999.99"), SEND + k * SEC)  # type: ignore[arg-type]
    assert rule.wants_out(a, D("1000.01"), SEND + 3 * SEC)  # type: ignore[arg-type]


def test_rules_built_for_separate_runs_share_no_latch() -> None:
    first, second = TargetExit(D(1000)), TargetExit(D(1000))
    assert first.wants_out(_pos("a"), D(1000), SEND)  # type: ignore[arg-type]
    assert not second.wants_out(_pos("a"), D(0), SEND)  # type: ignore[arg-type]
