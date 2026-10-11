"""Q5.6 exit "time", inside trade management (tests/execution stack). Slice 1: the rule.

Andre (2026-10-10, APPROVED): "if 10 minutes after the entry fill the position's open
profit is less than +$250, close the whole position. Otherwise the trade continues under
the baseline "trail" exit. 10 minutes and $250 live in config (approved values). Hard
loss stop unchanged."

Readings Q5.6-a (for Andre): open profit is trade management's figure, on the bid net of
round-trip fees (as for "target"); "10 minutes after the entry fill" is judged once, on
the first fresh quote trade management judges at or after entry fill + 10 minutes;
"less than" is strict, so exactly +$250.00 at 10:00 keeps the trade; a keep is final
(the baseline trail carries on, the rule never asks again); an out is held until the
position is gone.

Fixture arithmetic: entry 151.5, point value 20, one contract, fees 2.5 a side, so the
net open profit on a bid b is (b - 151.5) x 20 - 5. A bid of 164.25 is +250.00 exactly;
164.00, one option tick lower, is +245.00. Synthetic quotes say NOTHING about
profitability.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from footprint.backtest.exits import TimeExit, TimeExitSettings
from footprint.execution.management.malik import ExitReason, Manager
from tests.execution.conftest import Stack
from tests.execution.test_execution import FILLED_AT, filled_position

D = Decimal
SEC = timedelta(seconds=1)
TEN = timedelta(minutes=10)
KEEP = D("164.25")  # net +250.00
TICK_BELOW = D("164.00")  # net +245.00
DEEP = D("130")  # net -435.00, above the hard stop at 114
SETTINGS = TimeExitSettings(minutes=10, min_gain=D(250))


def _with_rule(stack: Stack, settings: TimeExitSettings = SETTINGS) -> TimeExit:
    rule = TimeExit(settings)
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
    return rule


def _at(stack: Stack, position: object, bid: Decimal, t: object) -> object:
    return stack.step(position, stack.quote(bid, bid + 1, t), (), t)  # type: ignore[arg-type]


def test_the_fixture_arithmetic(stack: Stack) -> None:
    position = filled_position(stack)
    assert position.entry_ts == FILLED_AT
    assert stack.manager._net_pnl(position, KEEP) == D("250.00")
    assert stack.manager._net_pnl(position, TICK_BELOW) == D("245.00")


def test_exactly_250_at_exactly_ten_minutes_keeps_the_trade_for_good(stack: Stack) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    assert _at(stack, position, KEEP, FILLED_AT + TEN) is None
    # the keep is final: later quotes below +$250 never close it by this rule
    for k in (1, 60, 600):
        assert _at(stack, position, DEEP, FILLED_AT + TEN + k * SEC) is None


def test_one_tick_below_at_ten_minutes_closes_the_whole_position_at_the_bid(
    stack: Stack,
) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    closed = _at(stack, position, TICK_BELOW, FILLED_AT + TEN)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.RULE, TICK_BELOW)  # type: ignore[attr-defined]
    assert closed.net_pnl == D("245.00")  # type: ignore[attr-defined]


def test_before_ten_minutes_nothing_happens_however_low(stack: Stack) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    for k in (1, 300, 599):
        assert _at(stack, position, DEEP, FILLED_AT + k * SEC) is None
    # the second just before is not the mark: then, at the mark, it is judged
    closed = _at(stack, position, DEEP, FILLED_AT + TEN)
    assert closed is not None
    assert closed.reason is ExitReason.RULE  # type: ignore[attr-defined]


def test_no_quote_at_the_mark_judges_the_first_fresh_quote_after_it(stack: Stack) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    assert _at(stack, position, DEEP, FILLED_AT + TEN - SEC) is None
    closed = _at(stack, position, TICK_BELOW, FILLED_AT + TEN + 7 * SEC)
    assert closed is not None
    assert closed.reason is ExitReason.RULE  # type: ignore[attr-defined]


def test_after_a_keep_the_baseline_trail_still_engages(stack: Stack) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    assert _at(stack, position, KEEP, FILLED_AT + TEN) is None
    at_target = D("201.75")  # net +1000.00, the configured profit target
    for k in (1, 2):
        assert _at(stack, position, at_target, FILLED_AT + TEN + k * SEC) is None
    assert stack.stops.current(position.position_id).trailing


def test_the_hard_stop_is_unchanged_and_comes_first(stack: Stack) -> None:
    _with_rule(stack)
    position = filled_position(stack)
    assert stack.stops.current(position.position_id).price == D(114)
    closed = _at(stack, position, D(114), FILLED_AT + TEN)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(114))  # type: ignore[attr-defined]


def test_configured_numbers_move_the_mark_and_the_bar(stack: Stack) -> None:
    _with_rule(stack, TimeExitSettings(minutes=3, min_gain=D(500)))
    position = filled_position(stack)
    three = timedelta(minutes=3)
    assert _at(stack, position, KEEP, FILLED_AT + three - SEC) is None
    closed = _at(stack, position, KEEP, FILLED_AT + three)  # +250 < +500
    assert closed is not None
    assert closed.reason is ExitReason.RULE  # type: ignore[attr-defined]


class _P:
    def __init__(self, pid: str) -> None:
        self.position_id = pid
        self.entry_ts = FILLED_AT


def test_an_out_is_held_and_verdicts_are_per_position() -> None:
    rule = TimeExit(SETTINGS)
    a, b = _P("a"), _P("b")
    mark = FILLED_AT + TEN
    assert rule.wants_out(a, D("249.99"), mark)  # type: ignore[arg-type]
    assert rule.wants_out(a, D(5000), mark + SEC)  # type: ignore[arg-type]  # held
    assert not rule.wants_out(b, D("250.00"), mark)  # type: ignore[arg-type]
    assert not rule.wants_out(b, D(-700), mark + SEC)  # type: ignore[arg-type]  # final
    # before the mark nothing is judged, and nothing is remembered
    c = _P("c")
    assert not rule.wants_out(c, D(-700), mark - SEC)  # type: ignore[arg-type]
    assert rule.wants_out(c, D("249.99"), mark)  # type: ignore[arg-type]
