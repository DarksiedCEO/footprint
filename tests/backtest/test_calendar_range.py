"""Delta review 2 (claude/audit-2026-10-07-delta2.md), N-2: outside the maintained
exchange calendar every day is closed (fail closed), so a backtest over such days
would run without an error and trade nothing. It is refused instead, before it runs."""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from footprint.backtest.engine import Backtest
from footprint.backtest.settings import BacktestSettings
from footprint.core.calendar import local_minute
from footprint.core.config import Profiles
from footprint.core.models import Bar
from tests.backtest.conftest import TRADE_FROM, workdir

WEEKS = timedelta(weeks=1)


def _shifted(history: tuple[Bar, ...], by: timedelta) -> list[Bar]:
    return [replace(bar, ts=bar.ts + by) for bar in history]


def _weeks_to(target_year: int, history: tuple[Bar, ...]) -> timedelta:
    """A whole number of weeks (so weekdays are kept) that moves every bar of
    `history` into `target_year`."""
    weeks = 0
    step = 1 if target_year > history[0].ts.year else -1
    while {(bar.ts + weeks * WEEKS).year for bar in (history[0], history[-1])} != {target_year}:
        weeks += step
    return weeks * WEEKS


def test_a_backtest_before_the_calendar_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    by = _weeks_to(profiles.instrument.calendar_from.year - 1, history)
    with pytest.raises(ValueError, match="calendar_from"):
        Backtest(
            profiles, settings, _shifted(history, by), workdir(tmp_path), trade_from=TRADE_FROM + by
        )


def test_a_backtest_past_the_calendar_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    by = _weeks_to(profiles.instrument.calendar_through.year + 1, history)
    with pytest.raises(ValueError, match="calendar_through"):
        Backtest(
            profiles, settings, _shifted(history, by), workdir(tmp_path), trade_from=TRADE_FROM + by
        )


def test_a_backtest_that_runs_past_the_calendar_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Starts inside, ends outside: the part outside would silently trade nothing."""
    by = _weeks_to(profiles.instrument.calendar_through.year + 1, history)
    late = replace(history[-1], ts=history[-1].ts + by)
    with pytest.raises(ValueError, match="calendar_through"):
        Backtest(profiles, settings, [*history, late], workdir(tmp_path), trade_from=TRADE_FROM)


def test_look_back_bars_before_the_calendar_are_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """backtest-history-v1 (BH-13), was "allowed": bars before `trade_from` are never
    traded, but the readers take sessions from them, and before calendar_from no holiday
    is known, so a holiday stub could be read as a session. Fail closed: refused."""
    by = _weeks_to(profiles.instrument.calendar_from.year - 1, history)
    early = replace(history[0], ts=history[0].ts + by)
    with pytest.raises(ValueError, match="before calendar_from"):
        Backtest(profiles, settings, [early, *history], workdir(tmp_path), trade_from=TRADE_FROM)


def test_a_backtest_inside_the_calendar_is_accepted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    Backtest(profiles, settings, list(history), workdir(tmp_path), trade_from=TRADE_FROM)


def _traded_days(profiles: Profiles, history: tuple[Bar, ...]) -> tuple[date, date]:
    """The local dates (prime session) of the first and last traded bars."""
    traded = [bar for bar in history if bar.ts >= TRADE_FROM]
    session = profiles.instrument.prime_session
    return local_minute(traded[0].ts, session)[0], local_minute(traded[-1].ts, session)[0]


def _calendar(profiles: Profiles, first: date, through: date) -> Profiles:
    instrument = replace(profiles.instrument, calendar_from=first, calendar_through=through)
    return replace(profiles, instrument=instrument)


def test_a_backtest_on_the_exact_first_and_last_calendar_days_is_accepted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Delta review 3, V14/V15: `calendar_from` and `calendar_through` are inclusive.
    A traded window that starts on the first maintained day and ends on the last is
    inside the calendar; one day less on either side refuses it."""
    # Only the traded bars: since BH-13 a look-back bar before calendar_from is refused
    # too, so the window under test is the whole series here.
    traded = [bar for bar in history if bar.ts >= TRADE_FROM]
    start, end = _traded_days(profiles, history)
    exact = _calendar(profiles, start, end)
    Backtest(exact, settings, traded, workdir(tmp_path / "exact"), trade_from=TRADE_FROM)
    day = timedelta(days=1)
    late_start = _calendar(profiles, start + day, max(end, start + day))
    with pytest.raises(ValueError, match="calendar_from"):
        Backtest(late_start, settings, traded, workdir(tmp_path / "a"), trade_from=TRADE_FROM)
    early_end = _calendar(profiles, min(start, end - day), end - day)
    with pytest.raises(ValueError, match="calendar_through"):
        Backtest(early_end, settings, traded, workdir(tmp_path / "b"), trade_from=TRADE_FROM)
