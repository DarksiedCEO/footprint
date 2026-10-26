"""Remediation items 7 and 8: an expired exchange calendar fails closed (no day past it
trades), and the US-holiday sessions are treated as closed (no-trade days)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from footprint.core.calendar import (
    SessionKind,
    calendar_warning,
    is_tradable_day,
    is_trading_day,
    previous_trading_day,
    session_at,
    trading_day_at,
)
from footprint.core.config import ConfigError, InstrumentProfile, Profiles, load_profiles
from footprint.execution.channels import is_critical
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack

PT = ZoneInfo("America/Los_Angeles")
US_HOLIDAY_SESSIONS = (
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2027, 1, 18),
    date(2027, 2, 15),
)


@pytest.fixture(scope="module")
def nq() -> InstrumentProfile:
    return load_profiles(REPO_ROOT / "config", "nq.yaml").instrument


def pt(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), PT).astimezone(UTC)


# --- 8: US-holiday sessions are closed ------------------------------------------------------


def test_the_us_holiday_sessions_are_the_no_trade_days(nq: InstrumentProfile) -> None:
    assert nq.no_trade_days == frozenset(US_HOLIDAY_SESSIONS)


@pytest.mark.parametrize("day", US_HOLIDAY_SESSIONS)
def test_a_us_holiday_session_is_closed_all_day(nq: InstrumentProfile, day: date) -> None:
    assert is_trading_day(day, nq)  # the exchange is open (a short session) ...
    assert not is_tradable_day(day, nq)  # ... Footprint is not
    for hour in (4, 6, 7, 9):
        assert session_at(pt(day, hour, 30), nq) is SessionKind.CLOSED
        assert trading_day_at(pt(day, hour, 30), nq) is None
    # The overnight session leading into it (from 15:00 PT the evening before, and
    # after midnight) belongs to it: closed too.
    assert session_at(pt(day - timedelta(days=1), 16, 30), nq) is SessionKind.CLOSED
    assert session_at(pt(day, 1), nq) is SessionKind.CLOSED


@pytest.mark.parametrize("day", US_HOLIDAY_SESSIONS)
def test_a_us_holiday_session_never_supplies_the_prior_day(
    nq: InstrumentProfile, day: date
) -> None:
    after = day + timedelta(days=1)
    while not is_tradable_day(after, nq):
        after += timedelta(days=1)
    assert previous_trading_day(after, nq) < day


def test_the_cash_market_half_days_still_trade_until_their_early_close(
    nq: InstrumentProfile,
) -> None:
    for day in (date(2026, 11, 27), date(2026, 12, 24)):
        assert is_tradable_day(day, nq)
        assert session_at(pt(day, 7), nq) is SessionKind.PRIME
        assert session_at(pt(day, 10, 1), nq) is SessionKind.CLOSED


def test_no_entry_is_sent_on_a_us_holiday_session(stack: Stack) -> None:
    from footprint.execution.executor.diego import EntryRefused

    day = DECISION_TIME.astimezone(PT).date()
    closed = replace(stack.profiles.instrument, no_trade_days=frozenset({day}))
    stack.executor._p = replace(stack.profiles, instrument=closed)
    with pytest.raises(EntryRefused):
        stack.executor.submit(stack.approved, (), DECISION_TIME)
    assert stack.broker.open_orders() == []


def test_no_entry_is_approved_on_a_us_holiday_session(stack: Stack) -> None:
    from footprint.decision.pipeline import Pipeline
    from tests.decision.scenario import chain_for, market

    day = DECISION_TIME.astimezone(PT).date()
    closed = replace(stack.profiles.instrument, no_trade_days=frozenset({day}))
    profiles = replace(stack.profiles, instrument=closed)
    bars = market()
    decision = Pipeline(profiles).decide(
        bars, chain_for(bars[-1].close), stack.ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    assert decision.approved is None


# --- 7: an expired calendar fails closed --------------------------------------------------------


def expired(profiles: Profiles) -> Profiles:
    day = DECISION_TIME.astimezone(PT).date()
    instrument = replace(
        profiles.instrument,
        calendar_through=day - timedelta(days=1),
        holidays=frozenset(d for d in profiles.instrument.holidays if d < day),
        early_closes={d: t for d, t in profiles.instrument.early_closes.items() if d < day},
        no_trade_days=frozenset(d for d in profiles.instrument.no_trade_days if d < day),
    )
    return replace(profiles, instrument=instrument)


def test_every_day_past_the_calendar_is_closed(nq: InstrumentProfile) -> None:
    through = nq.calendar_through
    assert session_at(pt(through, 7), nq) in (SessionKind.PRIME, SessionKind.CLOSED)
    day = through + timedelta(days=1)
    for _ in range(60):
        assert not is_tradable_day(day, nq)
        assert session_at(pt(day, 7), nq) is SessionKind.CLOSED
        assert session_at(pt(day, 1), nq) is SessionKind.CLOSED  # the overnight too
        day += timedelta(days=1)


def test_an_expired_calendar_approves_no_entry(stack: Stack) -> None:
    from footprint.decision.pipeline import Pipeline
    from tests.decision.scenario import chain_for, market

    profiles = expired(stack.profiles)
    bars = market()
    decision = Pipeline(profiles).decide(
        bars, chain_for(bars[-1].close), stack.ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    assert decision.approved is None


def test_an_expired_calendar_sends_no_entry(stack: Stack) -> None:
    from footprint.execution.executor.diego import EntryRefused

    stack.executor._p = expired(stack.profiles)
    with pytest.raises(EntryRefused):
        stack.executor.submit(stack.approved, (), DECISION_TIME)
    assert stack.broker.open_orders() == []
    assert stack.broker.positions() == {}


def test_the_expired_alarm_is_critical_and_says_no_new_trades(nq: InstrumentProfile) -> None:
    warning = calendar_warning(nq.calendar_through + timedelta(days=1), nq)
    assert warning is not None
    assert "NO NEW TRADES" in warning
    assert is_critical(warning)
    soon = calendar_warning(nq.calendar_through - timedelta(days=5), nq)
    assert soon is not None
    assert not is_critical(soon)  # the month's warning: push only, no call


# --- config: the no-trade list is validated ------------------------------------------------------


def _nq_with(tmp_path: Path, replace_text: tuple[str, str]) -> Path:
    config = tmp_path / "config"
    for f in (REPO_ROOT / "config").rglob("*.yaml"):
        target = config / f.relative_to(REPO_ROOT / "config")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.read_text())
    nq_path = config / "instruments" / "nq.yaml"
    text = nq_path.read_text()
    assert text.count(replace_text[0]) == 1
    nq_path.write_text(text.replace(*replace_text))
    return config


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("  - 2026-01-19   # Martin", "  - 2026-01-18   # Martin", "not a trading day"),
        ("  - 2026-01-19   # Martin", "  - 2026-01-01   # Martin", "both a holiday"),
        (
            "  - 2027-02-15   # Presidents",
            "  - 2027-04-05   # Presidents",
            "after calendar_through",
        ),
        ("  - 2026-01-19   # Martin", "  - 2026-02-16   # Martin", "duplicate"),
    ],
)
def test_a_bad_no_trade_day_is_refused(tmp_path: Path, old: str, new: str, why: str) -> None:
    config = _nq_with(tmp_path, (old, new))
    with pytest.raises(ConfigError, match=why):
        load_profiles(config, "nq.yaml")


def test_the_no_trade_list_cannot_be_left_out(tmp_path: Path) -> None:
    config = _nq_with(tmp_path, ("no_trade_days:", "no_trade_days_off:"))
    with pytest.raises(ConfigError):
        load_profiles(config, "nq.yaml")


def test_dropping_the_first_years_dates_is_refused(tmp_path: Path) -> None:
    """Audit 2026-10-07 A-8: the completeness check starts at a fixed `calendar_from`,
    not at the earliest date listed, so deleting a whole year cannot switch it off."""
    config = _nq_with(tmp_path, ("calendar_through:", "calendar_through:"))
    nq_path = config / "instruments" / "nq.yaml"
    kept = [
        line
        for line in nq_path.read_text().splitlines()
        if not line.lstrip().startswith(("- 2026-", "2026-"))
    ]
    nq_path.write_text("\n".join(kept) + "\n")
    with pytest.raises(ConfigError, match="2026-"):
        load_profiles(config, "nq.yaml")


def test_the_calendar_start_cannot_be_left_out(tmp_path: Path) -> None:
    config = _nq_with(tmp_path, ("calendar_from:", "calendar_from_off:"))
    with pytest.raises(ConfigError):
        load_profiles(config, "nq.yaml")


def test_the_calendar_start_cannot_be_after_its_end(tmp_path: Path) -> None:
    config = _nq_with(tmp_path, ("calendar_from: 2026-01-01", "calendar_from: 2027-04-01"))
    with pytest.raises(ConfigError, match="calendar_from is after calendar_through"):
        load_profiles(config, "nq.yaml")


def test_the_last_day_of_the_calendar_still_trades(nq: InstrumentProfile) -> None:
    """Independent audit A-15: `calendar_through` is inside the calendar (an ordinary
    weekday there trades); the day after is not."""
    through = nq.calendar_through
    assert through.weekday() < 5  # 2027-03-31 is a Wednesday, no listed closure
    assert through not in nq.holidays
    assert through not in nq.no_trade_days
    assert is_tradable_day(through, nq)
    assert session_at(pt(through, 7), nq) is SessionKind.PRIME


def test_a_date_before_the_calendar_start_is_refused(tmp_path: Path) -> None:
    """Delta review F-6 (A8b): a listed date before `calendar_from` is a config error."""
    config = _nq_with(tmp_path, ("  - 2026-01-01   # New Year's Day", "  - 2025-12-25   # moved"))
    with pytest.raises(ConfigError, match="before calendar_from"):
        load_profiles(config, "nq.yaml")


def test_every_day_before_the_calendar_is_closed(nq: InstrumentProfile) -> None:
    """Delta review F-6: fail closed on both ends. Before `calendar_from` no holiday or
    early close is known, so no day trades (a backtest over older data needs that
    year's calendar first)."""
    day = nq.calendar_from - timedelta(days=1)
    for _ in range(60):
        assert not is_tradable_day(day, nq)
        assert session_at(pt(day, 7), nq) is SessionKind.CLOSED
        day -= timedelta(days=1)
    first = nq.calendar_from
    while first.weekday() >= 5 or first in nq.holidays or first in nq.no_trade_days:
        first += timedelta(days=1)
    assert is_tradable_day(first, nq)  # the first ordinary day inside it trades
