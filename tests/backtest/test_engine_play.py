"""The play caller in the backtest (PC-2, PC-3, PC-5).

Written before the code. The backtest builds the live play caller on its own history
source: every bar closed by then, from the start of the data (the crew keeps its
history_minutes window). Each day's label is journaled once and counted in the readout;
a run with no TREND day says why instead of silently trading nothing. A label that turns
against an open trade never touches its exits.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.engine import Backtest
from footprint.backtest.readout import read_out, to_markdown
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar
from footprint.decision.play import PlayCaller, PlayCause
from tests.agents.test_regime_reader import SMALL, _decision
from tests.backtest.conftest import TRADE_FROM, extend, workdir
from tests.decision.plays import FixedPlay, Switch
from tests.decision.test_play import trend_day

D = Decimal
MINUTE = timedelta(minutes=1)
pytestmark = pytest.mark.real_play


def test_the_real_play_caller_stands_aside_on_the_short_scenario_and_says_so(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = extend(history, [D(10)] * 10)
    run = Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert run.trades == []
    assert run.fired == 0
    assert len(run.plays) == 1
    (play,) = run.plays
    assert play["cause"] == PlayCause.NO_DATA.value
    # Made at the decision minute: the first step reconciles, the next decides.
    on_time = TRADE_FROM + timedelta(seconds=settings.engine.substep_seconds)
    assert play["as_of"] == on_time.isoformat()
    readout = read_out(run, settings, {})
    assert readout.play_days == {"no_data": 1}
    assert any(w.startswith("no trading day was labelled TREND") for w in readout.why)
    assert "no_data 1" in " ".join(readout.why)
    text = to_markdown(readout)
    assert "## Play caller" in text
    assert "- no_data: 1" in text


def test_an_always_trend_stand_in_trades_exactly_as_before_the_wiring(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = extend(history, [D(-10)] * 15)
    play = FixedPlay(PlayCause.TREND)
    run = Backtest(
        profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM, play=play
    ).run()
    assert len(run.trades) == 1
    assert run.trades[0]["exit_reason"] == "STOP"
    assert run.plays == []  # a stand-in never fixes a day
    readout = read_out(run, settings, {})
    assert readout.play_days == {}
    assert not any("TREND" in w for w in readout.why)
    assert "- no day's label was journaled" in to_markdown(readout)


def test_a_label_turning_against_an_open_trade_never_touches_its_exits(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The same run twice: TREND throughout, and TREND only until the entry fills (then
    every play takes no entries). The trade, its exit and its P&L are identical."""
    moves = [D(0)] + [D(10)] * 16 + [D(-10)] * 12
    bars = extend(history, moves)
    steady = Backtest(
        profiles,
        settings,
        bars,
        workdir(tmp_path / "a"),
        trade_from=TRADE_FROM,
        play=FixedPlay(PlayCause.TREND),
    ).run()

    switch = Switch(PlayCause.CHOP)

    class FlipOnFill(Backtest):
        def _mark(self, t: datetime) -> None:
            if self.runner.position is not None:
                switch.flip()
            super()._mark(t)

    turned_bt = FlipOnFill(
        profiles, settings, bars, workdir(tmp_path / "b"), trade_from=TRADE_FROM, play=switch
    )
    turned = turned_bt.run()
    assert switch.cause is PlayCause.CHOP  # it did turn, while the trade was open
    assert len(steady.trades) == 1
    keys = ("entry_price", "exit_price", "exit_reason", "net_pnl", "entry_ts", "exit_ts")
    assert [{k: t[k] for k in keys} for t in turned.trades] == [
        {k: t[k] for k in keys} for t in steady.trades
    ]
    assert turned.final_equity == steady.final_equity


def test_the_play_callers_history_is_every_closed_bar_from_the_start(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    short = replace(settings, engine=replace(settings.engine, history_minutes=100))
    seen: list[tuple[datetime, int, int]] = []
    bars = extend(history, [D(1)] * 3)

    class Spy(Backtest):
        def _history_bars(self, now: datetime) -> tuple[Bar, ...]:
            out = super()._history_bars(now)
            closed = sum(1 for b in bars if b.ts + MINUTE <= now)
            seen.append((now, len(out), closed))
            assert all(b.ts + MINUTE <= now for b in out)
            assert out[0] == bars[0]
            return out

    Spy(profiles, short, bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert len(seen) == 1  # asked once: the day's label is fixed at the decision minute
    now, given, closed = seen[0]
    assert now == TRADE_FROM + timedelta(seconds=short.engine.substep_seconds)
    assert given == closed > short.engine.history_minutes


def test_a_trend_label_from_the_backtests_own_history_lets_the_crew_decide(
    profiles: Profiles, settings: BacktestSettings, tmp_path: Path
) -> None:
    """End to end with the real reader (small lookbacks): the backtest's own history
    source carries the reader's sessions though the crew's window is far shorter, the
    label is TREND, journaled once, and counted."""
    inst = profiles.instrument
    small = replace(profiles, regime=SMALL)
    bars = trend_day(inst)
    at = _decision(inst)
    # One more bar so the clock reaches the decision minute and decides on it.
    last = bars[-1]
    bars.append(Bar(last.symbol, at, last.close, last.close + 1, last.close - 1, last.close, 100))
    short = replace(settings, engine=replace(settings.engine, history_minutes=60))
    run = Backtest(small, short, bars, workdir(tmp_path), trade_from=at).run()
    assert [p["cause"] for p in run.plays] == ["trend"]
    assert run.plays[0]["entries_allowed"] is True
    assert read_out(run, short, {}).play_days == {"trend": 1}
    assert not any("no trading day" in w for w in read_out(run, short, {}).why)


def test_warm_up_days_are_reported(
    profiles: Profiles, settings: BacktestSettings, tmp_path: Path
) -> None:
    inst = profiles.instrument
    small = replace(profiles, regime=SMALL)
    caller = PlayCaller(SMALL, profiles.play, profiles.strategy.data, inst)
    day = inst.calendar_from + timedelta(days=5)
    while day.weekday() >= 5 or day in inst.holidays:
        day += timedelta(days=1)
    assert caller.call((), _decision(inst, day)).cause is PlayCause.WARM_UP
    at = _decision(inst, day)
    bars = [
        Bar(inst.symbol, at - MINUTE * k, D(100), D(101), D(99), D(100), 100)
        for k in range(5, 0, -1)
    ]
    bars.append(Bar(inst.symbol, at, D(100), D(101), D(99), D(100), 100))
    run = Backtest(small, settings, bars, workdir(tmp_path), trade_from=at).run()
    assert [p["cause"] for p in run.plays] == ["warm_up"]
    readout = read_out(run, settings, {})
    why = " ".join(readout.why)
    assert "warm_up 1" in why
    assert "calendar_from" in why
    assert "PC-3" in why
