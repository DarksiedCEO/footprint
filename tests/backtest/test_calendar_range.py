"""Delta review 2 (claude/audit-2026-10-07-delta2.md), N-2: outside the maintained
exchange calendar every day is closed (fail closed), so a backtest over such days
would run without an error and trade nothing. It is refused instead, before it runs."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from footprint.backtest.engine import Backtest
from footprint.backtest.settings import BacktestSettings
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


def test_look_back_bars_before_the_calendar_are_allowed(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Only the traded window must be inside the calendar: bars before `trade_from`
    are the readers' look-back, never traded."""
    by = _weeks_to(profiles.instrument.calendar_from.year - 1, history)
    early = replace(history[0], ts=history[0].ts + by)
    Backtest(profiles, settings, [early, *history], workdir(tmp_path), trade_from=TRADE_FROM)


def test_a_backtest_inside_the_calendar_is_accepted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    Backtest(profiles, settings, list(history), workdir(tmp_path), trade_from=TRADE_FROM)
