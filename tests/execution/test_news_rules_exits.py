"""Held positions under the scheduled windows (N3, N5, N-OPEN-2): every close goes
through the existing exit path: the resting stop's cancel, then a LIMIT sell, once."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from footprint.core.config import FomcMeeting, Profiles
from footprint.decision.news_rules import NewsRules
from footprint.decision.session import NewsBlackout, in_news_blackout
from footprint.execution.executor.diego import EntryOutcome, Position
from footprint.execution.management.malik import ExitReason
from footprint.execution.orders import OrderState, OrderType, Side
from tests.decision.scenario import LAST_DAY, PT
from tests.execution.conftest import SEND, Stack

D = Decimal
SEC = timedelta(seconds=1)
FILLED_AT = SEND + 10 * SEC


def filled_position(stack: Stack) -> Position:
    ticket = stack.executor.submit(stack.approved, (), SEND)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stack.quote(150, D("151.5"), FILLED_AT)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    assert ticket.outcome is EntryOutcome.PROTECTED, ticket.outcome
    assert ticket.position is not None
    return ticket.position


def fomc_on(profiles: Profiles, day: date) -> NewsRules:
    """The repo's rules with `day` as an FOMC decision day (in place of the same year's
    September meeting, so the calendar stays complete)."""
    meetings = tuple(
        FomcMeeting(day - timedelta(days=1), day, True)
        if m.decision_day == date(2026, 9, 16)
        else m
        for m in profiles.news_rules.fomc.meetings
    )
    fomc = replace(profiles.news_rules.fomc, meetings=meetings)
    return NewsRules(replace(profiles.news_rules, fomc=fomc), profiles.instrument)


def pt(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(LAST_DAY, time(hh, mm, ss), PT).astimezone(UTC)


def sells(stack: Stack, position: Position) -> list[str]:
    return [
        oid
        for oid, status in stack.broker._orders.items()
        if oid.startswith(position.position_id) and status.order.side is Side.SELL
    ]


def test_n3_a_position_held_into_the_open_is_closed_at_0625_pt(stack: Stack) -> None:
    """No trade is carried through the cash open: a position still held the next
    morning (none can be today: prime positions are closed at the prime close) is
    closed when the 06:25-06:32 PT window starts, through a LIMIT sell, once."""
    # N4 ends at the 08:30 ET window here, so only N3 is in force at 06:25 PT
    settings = replace(stack.profiles.news_rules, pre_news_until="8:30 ET releases (N1)")
    rules = NewsRules(settings, stack.profiles.instrument)
    position = filled_position(stack)
    nxt = LAST_DAY + timedelta(days=1)
    before = datetime.combine(nxt, time(6, 24, 59), PT).astimezone(UTC)
    assert in_news_blackout(before, rules.windows(before)) is None
    quote = stack.quote(160, 161, before)
    assert stack.step(position, quote, rules.windows(before), before) is None
    t = datetime.combine(nxt, time(6, 25), PT).astimezone(UTC)
    window = in_news_blackout(t, rules.windows(t))
    assert window is not None
    assert window.name == "cash open (N3)"
    closed = stack.step(position, stack.quote(160, 161, t), rules.windows(t), t)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS
    exit_order = stack.broker.status(f"{position.position_id}-exit-1").order
    assert exit_order.order_type is OrderType.LIMIT  # never a market order
    assert exit_order.quantity == position.contracts
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.CANCELLED
    assert sorted(sells(stack, position)) == [  # one stop, one exit: never sold twice
        f"{position.position_id}-exit-1",
        f"{position.position_id}-stop",
    ]


def test_n5_a_held_position_is_flat_by_1055_pt_on_an_fomc_day(stack: Stack) -> None:
    rules = fomc_on(stack.profiles, LAST_DAY)
    position = filled_position(stack)
    before = pt(10, 54, 59)
    assert (
        stack.step(position, stack.quote(160, 161, before), rules.windows(before), before) is None
    )
    t = pt(10, 55)
    closed = stack.step(position, stack.quote(160, 161, t), rules.windows(t), t)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS
    exit_order = stack.broker.status(f"{position.position_id}-exit-1").order
    assert exit_order.order_type is OrderType.LIMIT  # never a market order
    assert exit_order.quantity == position.contracts
    assert stack.broker.status(f"{position.position_id}-stop").state is OrderState.CANCELLED
    # one stop and one exit: never sold twice
    assert sorted(sells(stack, position)) == [
        f"{position.position_id}-exit-1",
        f"{position.position_id}-stop",
    ]


def test_the_rules_leave_a_position_alone_outside_every_window(stack: Stack) -> None:
    rules = NewsRules(stack.profiles.news_rules, stack.profiles.instrument)  # 15 Sep: day one
    position = filled_position(stack)
    t = pt(10, 55)
    assert stack.step(position, stack.quote(160, 161, t), rules.windows(t), t) is None


def test_n_open_2_a_daily_window_without_the_switch_starts_no_time_stop(stack: Stack) -> None:
    """The same trade as test_news_time_stop, after a window that does not carry it."""
    window = NewsBlackout("daily", SEND - 120 * SEC, SEND - 60 * SEC, time_stop=False)
    position = filled_position(stack)
    up = (D("161.5"), D("162.5"))  # short of the time stop's gain
    ten = position.entry_ts + 10 * 60 * SEC
    assert stack.step(position, stack.quote(*up, ten), (window,), ten) is None


def test_n_open_2_a_daily_window_with_the_switch_starts_the_time_stop(stack: Stack) -> None:
    window = NewsBlackout("daily", SEND - 120 * SEC, SEND - 60 * SEC, time_stop=True)
    position = filled_position(stack)
    up = (D("161.5"), D("162.5"))
    ten = position.entry_ts + 10 * 60 * SEC
    closed = stack.step(position, stack.quote(*up, ten), (window,), ten)
    assert closed is not None
    assert closed.reason is ExitReason.NEWS_TIME_STOP
