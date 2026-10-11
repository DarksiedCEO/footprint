"""Q5.0 rules seam, exit side: a named exit rule can only ADD a way out.

The rule is asked last, after every existing reason (hard stop, gap, close, news,
news time stop, daily and weekly loss, kill switch), and only on a fresh quote. It
never replaces, moves or loosens the hard stop; with no rule, Malik is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from footprint.execution.executor.diego import Position
from footprint.execution.management.malik import ExitReason, Manager
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position

D = Decimal
SEC = timedelta(seconds=1)


class FixedExit:
    """A stand-in exit rule: always answers `out` and records what it was asked."""

    def __init__(self, out: bool) -> None:
        self.out = out
        self.asked: list[tuple[str, Decimal, datetime]] = []

    def wants_out(self, position: Position, net_open: Decimal, now: datetime) -> bool:
        self.asked.append((position.position_id, net_open, now))
        return self.out


def _with_rule(stack: Stack, rule: FixedExit) -> None:
    stack.manager = Manager(
        stack.profiles,
        stack.manager._broker,
        stack.stops,
        stack.ledger,
        stack.halt,
        stack.book,
        stack.alarms.append,
        exit_rule=rule,
    )


def test_a_rule_that_wants_out_closes_the_trade_at_the_bid(stack: Stack) -> None:
    rule = FixedExit(True)
    _with_rule(stack, rule)
    position = filled_position(stack)  # entry 151.5, stop 114
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, D(160))
    # Asked once, with the open profit net of round-trip fees: (160 - 151.5) x 20 - 5.
    assert rule.asked == [(position.position_id, D("165.0"), t)]


def test_a_rule_that_stays_in_changes_nothing(stack: Stack) -> None:
    rule = FixedExit(False)
    _with_rule(stack, rule)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(160, 161, t), (), t) is None
    assert len(rule.asked) == 1
    assert stack.stops.current(position.position_id).price == D(114)


def test_the_hard_stop_comes_before_the_rule(stack: Stack) -> None:
    rule = FixedExit(True)
    _with_rule(stack, rule)
    position = filled_position(stack)
    t = SEND + 10 * SEC
    closed = stack.step(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(114))
    assert rule.asked == []


def test_every_existing_reason_comes_before_the_rule(stack: Stack) -> None:
    rule = FixedExit(True)
    _with_rule(stack, rule)
    position = filled_position(stack)
    stack.ledger.latch_kill_switch("test")
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(160, 161, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.KILL_SWITCH
    assert rule.asked == []


def test_the_rule_is_never_asked_on_a_stale_quote(stack: Stack) -> None:
    rule = FixedExit(True)
    _with_rule(stack, rule)
    position = filled_position(stack)
    old = SEND + 30 * SEC
    stack.quote(160, 161, old)
    late = old + (stack.profiles.execution.quotes.max_age_seconds + 1) * SEC
    assert stack.manager.manage(position, (), late) is None
    assert rule.asked == []


def test_with_no_rule_the_manager_never_exits_for_a_rule(stack: Stack) -> None:
    position = filled_position(stack)
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(160, 161, t), (), t) is None
