"""Q5.5 exit "target", inside trade management (tests/execution stack).

Andre (2026-10-10): "when the position's open profit reaches +$1,000 (the existing
configured target), close ALL contracts; no trail, no scale-out, no runner. Hard loss
stop unchanged." Readings Q5.5-a (for Andre): open profit is the position's profit on
the bid net of round-trip fees (the same figure the trail and every risk check use);
"reaches" is at or above, so exactly +$1,000.00 closes; ALL contracts is the one
position (scale-out is not built, max_contracts is 1). The rule only adds a way out:
the hard stop and every existing reason come first, and the exit is the existing one.

Fixture arithmetic: entry 151.5, point value 20, one contract, fees 2.5 a side, so the
net open profit on a bid b is (b - 151.5) x 20 - 5. A bid of 201.75 is +1000.00
exactly; 201.50, one option tick lower, is +995.00. Synthetic quotes say NOTHING about
profitability.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from footprint.backtest.exits import TargetExit
from footprint.backtest.rules import EXIT_RULES
from footprint.backtest.settings import load_backtest
from footprint.core.config import Profiles
from footprint.execution.management.malik import ExitReason, Manager
from tests.conftest import REPO_ROOT
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position

D = Decimal
SEC = timedelta(seconds=1)
AT_TARGET = D("201.75")  # net +1000.00
TICK_BELOW = D("201.50")  # net +995.00


def _with_rule(stack: Stack, profiles: Profiles) -> None:
    rule = EXIT_RULES["target"](
        profiles, load_backtest(REPO_ROOT / "config" / "backtest.yaml").rules
    )
    assert rule is not None
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


def test_the_fixture_arithmetic(stack: Stack) -> None:
    assert stack.profiles.risk.profit_target == D(1000)
    position = filled_position(stack)
    assert stack.manager._net_pnl(position, AT_TARGET) == D("1000.00")
    assert stack.manager._net_pnl(position, TICK_BELOW) == D("995.00")


def test_exactly_at_the_target_closes_the_whole_position_at_the_bid(stack: Stack) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    closed = stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, AT_TARGET)
    assert closed.net_pnl == D("1000.00")  # the whole position, net of fees


def test_one_tick_below_the_target_stays_in_and_moves_nothing(stack: Stack) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    for k in (30, 31, 32):
        t = SEND + k * SEC
        assert stack.step(position, stack.quote(TICK_BELOW, TICK_BELOW + 1, t), (), t) is None
    assert stack.stops.current(position.position_id).price == D(114)
    assert not stack.stops.current(position.position_id).trailing


def test_without_the_rule_the_same_quotes_engage_the_trail(stack: Stack) -> None:
    """The baseline, for contrast with the next test: two quotes at the target engage
    the trail and the trade stays in."""
    position = filled_position(stack)
    for k in (30, 31):
        t = SEND + k * SEC
        assert stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t) is None
    assert stack.stops.current(position.position_id).trailing  # baseline: trail engaged


def test_with_the_rule_the_trail_never_engages(stack: Stack) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    stack.step(position, stack.quote(TICK_BELOW, TICK_BELOW + 1, t), (), t)
    assert not stack.stops.current(position.position_id).trailing
    t = SEND + 31 * SEC
    closed = stack.step(position, stack.quote(AT_TARGET, AT_TARGET + 1, t), (), t)
    assert closed is not None
    assert closed.reason is ExitReason.RULE


def test_the_hard_stop_is_unchanged_and_comes_first(stack: Stack) -> None:
    _with_rule(stack, stack.profiles)
    position = filled_position(stack)
    assert stack.stops.current(position.position_id).price == D(114)
    t = SEND + 10 * SEC
    closed = stack.step(position, stack.quote(114, 116, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(114))


def test_the_target_is_the_configured_one(stack: Stack) -> None:
    """Built from risk.profit_target: a different configured target moves the exit."""
    half = replace(stack.profiles, risk=replace(stack.profiles.risk, profit_target=D(500)))
    _with_rule(stack, half)
    position = filled_position(stack)
    t = SEND + 30 * SEC
    bid = D("176.75")  # (176.75 - 151.5) x 20 - 5 = +500.00
    closed = stack.step(position, stack.quote(bid, bid + 1, t), (), t)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, bid)


def test_the_rule_reads_only_the_open_profit() -> None:
    rule = TargetExit(D(1000))
    position = object()
    now = SEND
    assert rule.wants_out(position, D("1000.00"), now)  # type: ignore[arg-type]
    assert rule.wants_out(position, D("1000.01"), now)  # type: ignore[arg-type]
    assert not rule.wants_out(position, D("999.99"), now)  # type: ignore[arg-type]
    assert not rule.wants_out(position, D(-750), now)  # type: ignore[arg-type]
