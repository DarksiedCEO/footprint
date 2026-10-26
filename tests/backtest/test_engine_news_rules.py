"""The scheduled news windows in the backtest (N1-N6). The backtest always runs them, on
top of any --blackouts CSV, exactly as live does (decision/news_rules.with_news_rules).
Other backtest tests get a stand-in with no scheduled windows (tests/backtest/conftest.py),
as they do for the play caller: these are marked `real_news`."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
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
from tests.agents.test_regime_reader import SMALL
from tests.backtest.conftest import ENTRY_DIP, TRADE_FROM, extend, workdir
from tests.decision.scenario import DECISION_TIME, LAST_DAY, PT

D = Decimal
MINUTE = timedelta(minutes=1)
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


# --- review round 1 M1: the production default path (real play caller + real rules) -----

LATE_DECISION = DECISION_TIME + 5 * MINUTE  # 07:06 PT = 10:06 ET, after N1 10:00 and N4


def _late(history: tuple[Bar, ...], moves: list[Decimal]) -> list[Bar]:
    """The scenario (its 10:01 ET long is vetoed), then a second volume spike at 07:04 PT
    and its follow-through at 07:05, so a long decides at 07:06 PT; then one bar per
    move, the first dipping so the resting entry fills (as conftest.extend)."""
    bars = list(history)
    price = bars[-1].close
    shape = [(D(1), 100), (D(1), 100), (D(1), 100), (D(3), 300), (D(1), 100)]
    for i, (move, volume) in enumerate(shape):
        o, c = price, price + move
        ts = DECISION_TIME + MINUTE * i
        bars.append(Bar("NQ", ts, o, max(o, c) + 1, min(o, c) - 1, c, volume))
        price = c
    for i, move in enumerate(moves):
        o, c = price, price + move
        low = min(o, c) - (ENTRY_DIP if i == 0 else D(1))
        bars.append(Bar("NQ", LATE_DECISION + MINUTE * i, o, max(o, c) + 1, low, c, 100))
        price = c
    return bars


@pytest.mark.real_play
def test_production_path_vetoes_the_1001_et_entry(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The real play caller (small lookbacks the scenario can carry) labels the day TREND,
    and the real rules veto the 10:01 ET long: nothing trades."""
    small = replace(profiles, regime=SMALL)
    bars = extend(history, [D(-10)] * 15)
    run = Backtest(small, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert [p["cause"] for p in run.plays] == ["trend"]
    assert run.trades == []
    names = {
        d["precheck"]["blackout"]["name"]  # type: ignore[index]
        for d in _decisions(tmp_path)
        if d["precheck"]["blackout"] is not None  # type: ignore[index]
    }
    assert names == {"10:00 ET releases (N1)"}


@pytest.mark.real_play
def test_production_path_trades_a_1006_et_entry_and_its_time_stop_follows_n_open_2(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The same day with a second long at 10:06 ET: allowed. Under the built N-OPEN-2 default
    the flat trade is closed by the news time stop; under the narrow reading (only after
    the 8:30 window) the same trade is not."""
    small = replace(profiles, regime=SMALL)
    bars = _late(history, [D(0)] * 20)
    trade_from = TRADE_FROM  # from 10:00 ET, so the play caller labels the day on time
    built = Backtest(small, settings, bars, workdir(tmp_path / "a"), trade_from=trade_from).run()
    assert [p["cause"] for p in built.plays] == ["trend"]
    assert len(built.trades) == 1
    (trade,) = built.trades
    assert datetime.fromisoformat(str(trade["entry_ts"])) >= LATE_DECISION
    assert trade["exit_reason"] == "NEWS_TIME_STOP"
    stop = timedelta(minutes=small.execution.news.time_stop_minutes)
    held = datetime.fromisoformat(str(trade["exit_ts"])) - datetime.fromisoformat(
        str(trade["entry_ts"])
    )
    assert held == stop

    s = small.news_rules
    events = tuple(replace(e, time_stop=e.name == "8:30 ET releases (N1)") for e in s.daily_events)
    narrow = replace(small, news_rules=replace(s, daily_events=events, pre_news_time_stop=False))
    other = Backtest(narrow, settings, bars, workdir(tmp_path / "b"), trade_from=trade_from).run()
    assert len(other.trades) == 1
    assert other.trades[0]["entry_ts"] == trade["entry_ts"]
    assert other.trades[0]["exit_reason"] == "END_OF_DATA"  # held to the end of the bars


# --- review round 1 C3: the readout names the rules in force and their values ---------

SHIPPED_LINES = {
    "news rules": "config/news.yaml (always on, every calendar day, beside any CSV)",
    "news.timezone": "America/New_York",
    "news.lead_minutes": "5",
    "news.lag_minutes": "2",
    "news.pre_news_until": "10:00 ET releases (N1)",
    "news.pre_news_time_stop": "true",
    "news.overnight_flat_from": "15:00",
    "news window 8:30 ET releases (N1)": (
        "08:25-08:32 America/New_York (whole end minute); time stop after: on"
    ),
    "news window cash open (N3)": (
        "09:25-09:32 America/New_York (whole end minute); time stop after: on"
    ),
    "news window 10:00 ET releases (N1)": (
        "09:55-10:02 America/New_York (whole end minute); time stop after: on"
    ),
    "news window no entries before news (N4) until 10:00 ET releases (N1)": (
        "04:00 America/Los_Angeles-10:02 America/New_York (whole end minute); time stop after: on"
    ),
    "news window overnight flat window (N-OPEN-3)": (
        "15:00-03:59 America/Los_Angeles (whole end minute); time stop after: on"
    ),
    "news.fomc_flat_from": "13:55",
    "news.fomc_warn_days": "30",
    "fomc calendar": (
        "config/fomc.yaml: valid 2017-01-01 to 2027-12-31; source "
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm read 2026-10-26; "
        "max_gap_days 60"
    ),
    "fomc days": (
        "78 verified decision days (N5 window), 9 UNVERIFIED decision days, 1 cancelled "
        "meeting, 10 intermeeting days (each a whole-day blackout)"
    ),
    "news window FOMC decision day (N5)": (
        "13:55 America/New_York-the prime close America/Los_Angeles (whole end minute) on "
        "each verified decision day; time stop after: on"
    ),
    "news window FOMC whole days (N6)": (
        "00:00-23:59 America/New_York on each UNVERIFIED or cancelled decision day, each "
        "intermeeting day, and every day outside 2017-01-01..2027-12-31; time stop after: on"
    ),
}


def test_the_readout_lines_for_the_shipped_rules(profiles: Profiles) -> None:
    assert news_rules.news_assumptions(profiles, Path("config/news.yaml")) == SHIPPED_LINES


def test_the_readout_lines_tell_the_narrow_n_open_2_run_apart(profiles: Profiles) -> None:
    s = profiles.news_rules
    events = tuple(replace(e, time_stop=e.name == "8:30 ET releases (N1)") for e in s.daily_events)
    narrow = replace(profiles, news_rules=replace(s, daily_events=events, pre_news_time_stop=False))
    lines = news_rules.news_assumptions(narrow, Path("config/news.yaml"))
    changed = {k for k in lines if lines[k] != SHIPPED_LINES[k]}
    assert changed == {
        "news.pre_news_time_stop",
        "news window cash open (N3)",
        "news window 10:00 ET releases (N1)",
        "news window no entries before news (N4) until 10:00 ET releases (N1)",
    }
    assert lines["news window 10:00 ET releases (N1)"].endswith("time stop after: off")
    assert lines["news.pre_news_time_stop"] == "false"


def test_the_readout_lines_follow_the_edges(profiles: Profiles) -> None:
    edges = replace(profiles.news_rules, lead_minutes=7, lag_minutes=4)
    lines = news_rules.news_assumptions(replace(profiles, news_rules=edges), Path("n.yaml"))
    assert lines["news rules"].startswith("n.yaml ")
    assert (lines["news.lead_minutes"], lines["news.lag_minutes"]) == ("7", "4")
    assert lines["news window 8:30 ET releases (N1)"].startswith("08:23-08:34 ")
    assert (
        "-10:04 America/New_York"
        in lines["news window no entries before news (N4) until 10:00 ET releases (N1)"]
    )


def test_the_backtest_cli_readout_carries_the_rules(tmp_path: Path) -> None:
    """End to end through the CLI (real rules): readout.md and readout.json list them,
    with the config directory the run was given."""
    from tests.backtest.test_rules_seam import _run

    out, printed = _run(tmp_path)
    text = (out / "readout.md").read_text(encoding="utf-8")
    data = json.loads((out / "readout.json").read_text(encoding="utf-8"))
    source = str(tmp_path / "config" / "news.yaml")
    expected = {
        **SHIPPED_LINES,
        "news rules": SHIPPED_LINES["news rules"].replace("config/news.yaml", source),
        "fomc calendar": SHIPPED_LINES["fomc calendar"].replace(
            "config/fomc.yaml", str(tmp_path / "config" / "fomc.yaml")
        ),
    }
    for key, value in expected.items():
        assert f"- {key}: {value}" in text
        assert f"- {key}: {value}" in printed
    assert "- news blackouts CSV: NONE supplied" in text
    assert {k: v for k, v in data["assumptions"].items() if k in expected} == expected


def test_the_backtest_cli_readout_names_a_csv_beside_the_rules(tmp_path: Path) -> None:
    from tests.backtest.test_rules_seam import _run

    csv = tmp_path / "news.csv"
    csv.write_text("name,start,end\nCPI,2026-09-14T08:25:00-04:00,2026-09-14T08:40:00-04:00\n")
    out, _ = _run(tmp_path, "--blackouts", str(csv))
    text = (out / "readout.md").read_text(encoding="utf-8")
    assert f"- news blackouts CSV: {csv}\n" in text
    assert f"- news rules: {tmp_path / 'config' / 'news.yaml'} (always on" in text
