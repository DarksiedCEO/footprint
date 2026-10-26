"""The scheduled news windows in the backtest (N1-N4). The backtest always runs them, on
top of any --blackouts CSV, exactly as live does (decision/news_rules.with_news_rules).
Other backtest tests get a stand-in with no scheduled windows (tests/backtest/conftest.py),
as they do for the play caller: these are marked `real_news`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
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
