"""The scheduled news windows in the backtest (N1-N6). The backtest always runs them, on
top of any --blackouts CSV, exactly as live does (decision/news_rules.with_news_rules).
Other backtest tests get a stand-in with no scheduled windows (tests/backtest/conftest.py),
as they do for the play caller: these are marked `real_news`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest import engine
from footprint.backtest.engine import Backtest
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar
from footprint.decision import news_rules
from footprint.decision.session import NewsBlackout
from tests.backtest.conftest import TRADE_FROM, extend, workdir
from tests.decision.scenario import LAST_DAY, PT

D = Decimal
pytestmark = pytest.mark.real_news


def _decisions(tmp_path: Path) -> list[dict[str, object]]:
    path = workdir(tmp_path) / "journal" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_ten_oclock_window_vetoes_the_scenario_entry(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The scenario's long fires at 07:01 PT (10:01 ET), inside N1's 09:55-10:02 ET
    window: with the real rules it is vetoed, and the record names the window."""
    bars = extend(history, [D(-10)] * 15)
    run = Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert run.trades == []
    vetoed = [
        d
        for d in _decisions(tmp_path)
        if "NEWS_BLACKOUT" in d["precheck"]["vetoes"]  # type: ignore[index]
    ]
    assert vetoed
    names = {d["precheck"]["blackout"]["name"] for d in vetoed}  # type: ignore[index]
    assert "10:00 ET releases (N1)" in names or any("(N4)" in n for n in names)
    last = max(datetime.fromisoformat(str(d["as_of"])) for d in vetoed)
    end_n1 = datetime.combine(LAST_DAY, time(7, 3), PT).astimezone(UTC)
    assert last < end_n1  # nothing is vetoed by the scheduled windows after 10:02:59 ET


def test_the_csv_windows_still_apply_beside_the_rules(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
) -> None:
    csv = NewsBlackout(
        "CSV release",
        datetime.combine(LAST_DAY, time(7, 3), PT).astimezone(UTC),
        datetime.combine(LAST_DAY, time(7, 10), PT).astimezone(UTC),
    )
    bars = extend(history, [D(-10)] * 15)
    Backtest(
        profiles,
        settings,
        bars,
        workdir(tmp_path),
        blackouts=lambda now: (csv,),
        trade_from=TRADE_FROM,
    ).run()
    names = {
        d["precheck"]["blackout"]["name"]  # type: ignore[index]
        for d in _decisions(tmp_path)
        if d["precheck"]["blackout"] is not None  # type: ignore[index]
    }
    assert "CSV release" in names
    assert "10:00 ET releases (N1)" in names or any("(N4)" in n for n in names)


def test_the_backtest_builds_the_rules_from_its_profiles(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Profiles] = []
    real = news_rules.with_news_rules

    def spy(p: Profiles, extra):  # type: ignore[no-untyped-def]
        seen.append(p)
        return real(p, extra)

    monkeypatch.setattr(engine, "with_news_rules", spy)
    Backtest(profiles, settings, extend(history, [D(1)]), workdir(tmp_path), trade_from=TRADE_FROM)
    assert seen == [profiles]


def test_a_backtest_past_the_fomc_calendar_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """N6, like D34: a run trading past valid_through would trade blind. The exchange
    calendar ends first today, so a shorter FOMC calendar stands in."""
    fomc = profiles.news_rules.fomc
    short = _replace_fomc(profiles, valid_through=date(2026, 9, 14))
    with pytest.raises(ValueError, match="FOMC calendar"):
        Backtest(short, settings, extend(history, [D(1)]), workdir(tmp_path), trade_from=TRADE_FROM)
    assert fomc.valid_through > LAST_DAY


def test_a_backtest_before_the_fomc_calendar_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    late = _replace_fomc(profiles, valid_from=date(2026, 9, 16))
    with pytest.raises(ValueError, match="FOMC calendar starts 2026-09-16"):
        Backtest(late, settings, extend(history, [D(1)]), workdir(tmp_path), trade_from=TRADE_FROM)


def test_a_backtest_inside_the_fomc_calendar_runs(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    exact = _replace_fomc(profiles, valid_from=date(2026, 9, 15), valid_through=date(2026, 9, 15))
    Backtest(exact, settings, extend(history, [D(1)]), workdir(tmp_path), trade_from=TRADE_FROM)


def _replace_fomc(
    profiles: Profiles, valid_from: date | None = None, valid_through: date | None = None
) -> Profiles:
    from dataclasses import replace

    fomc = profiles.news_rules.fomc
    lo = fomc.valid_from if valid_from is None else valid_from
    hi = fomc.valid_through if valid_through is None else valid_through
    meetings = tuple(m for m in fomc.meetings if lo <= m.decision_day <= hi)
    new = replace(fomc, meetings=meetings, valid_from=lo, valid_through=hi)
    return replace(profiles, news_rules=replace(profiles.news_rules, fomc=new))


def test_the_shipped_fomc_calendar_starts_with_bh14s_history(profiles: Profiles) -> None:
    """BH-14: history from 2017 runs; a day before it is refused."""
    from footprint.decision.news_rules import check_fomc_span

    first = datetime(2017, 1, 3, 15, tzinfo=UTC)
    check_fomc_span(profiles, first, datetime(2027, 12, 31, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match="FOMC calendar starts 2017-01-01"):
        check_fomc_span(profiles, datetime(2016, 12, 31, 15, tzinfo=UTC), first)


def test_l1_the_span_check_reads_the_et_date(profiles: Profiles) -> None:
    """2017-01-01 02:00Z is 2016-12-31 21:00 ET: before the calendar (review L1)."""
    from footprint.decision.news_rules import check_fomc_span

    last = datetime(2017, 1, 3, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="trades from 2016-12-31"):
        check_fomc_span(profiles, datetime(2017, 1, 1, 2, tzinfo=UTC), last)
    check_fomc_span(profiles, datetime(2017, 1, 1, 5, tzinfo=UTC), last)  # 00:00 ET
    with pytest.raises(ValueError, match="trades until 2028-01-01"):
        check_fomc_span(profiles, last, datetime(2028, 1, 1, 5, tzinfo=UTC))
    check_fomc_span(profiles, last, datetime(2028, 1, 1, 4, 59, tzinfo=UTC))  # 23:59 ET
