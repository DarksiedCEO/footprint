"""Cross-PR interactions on the integrated tree (integration-v1, review round 1, L1).

Each PR certified its own behaviour; these pin how two of them combine:

- #42's decay exit (DX-1) under #47's price-level rescaling (BH-2): with the decay exit
  on, a trade anchored at the reference has the un-rescaled dollars, and the same
  percentage path at another price level has the same dollars. The decay exit reads the
  rescaled option quotes, so its decision must not depend on the data's level.
- #44's play caller with #47's roll gate (BH-9): a roll session takes no entry whatever
  the day's play label is (the roll console stops it, not the label).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.engine import Backtest, BacktestRun
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar
from footprint.decision.play import PlayCause
from tests.backtest.conftest import TRADE_FROM, workdir
from tests.backtest.test_backtest_history import REF, dollars, ridden, run, scaled, times
from tests.backtest.test_engine import with_decay_exit
from tests.backtest.test_roll_sessions import bars as stopped_bars
from tests.backtest.test_roll_sessions import blocking
from tests.decision.plays import FixedPlay
from tests.decision.scenario import LAST_DAY

D = Decimal


# --- decay exit (#42) x price-level rescaling (#47) --------------------------------------


def _decay(result: BacktestRun) -> list[tuple[object, ...]]:
    got = dollars(result)
    assert len(got) == 1, got
    assert got[0][0] == "DECAY_EXIT"
    return got


def test_with_the_decay_exit_on_a_trade_at_the_reference_has_the_unscaled_dollars(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    p = with_decay_exit(profiles)
    bars = ridden(history)
    anchor = history[-1].close  # the entry is decided at the open of the next bar
    _, plain = run(p, scaled(settings, None), bars, tmp_path / "plain")
    _, at_ref = run(p, scaled(settings, anchor), bars, tmp_path / "ref")
    assert _decay(at_ref) == _decay(plain)
    assert at_ref.final_equity == plain.final_equity


@pytest.mark.parametrize("k", [D("0.1"), D("0.5"), D(2)])
def test_with_the_decay_exit_on_the_same_percentage_path_has_the_same_dollars(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    k: Decimal,
) -> None:
    p = with_decay_exit(profiles)
    bars = ridden(history)
    s = scaled(settings, REF)
    _, here = run(p, s, bars, tmp_path / "here")
    _, there = run(p, s, times(bars, k), tmp_path / "there")
    assert _decay(there) == _decay(here)
    net = D(str(here.trades[0]["net_pnl"]))
    assert D(0) < net < profiles.risk.profit_target  # a small gain, closed by the decay exit
    assert there.final_equity == here.final_equity


# --- play caller (#44) x roll gate (#47) ------------------------------------------------


def _rolled(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp: Path,
    cause: PlayCause,
    rolls: frozenset[date],
) -> tuple[Backtest, BacktestRun]:
    bt = Backtest(
        profiles,
        blocking(settings),
        stopped_bars(history),
        workdir(tmp),
        trade_from=TRADE_FROM,
        roll_sessions=rolls,
        play=FixedPlay(cause),
    )
    return bt, bt.run()


def test_off_a_roll_session_a_trend_play_trades(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The control: the same bars, TREND, no roll listed: the one trade is made."""
    _, result = _rolled(profiles, settings, history, tmp_path, PlayCause.TREND, frozenset())
    assert len(result.trades) == 1


@pytest.mark.parametrize("cause", [PlayCause.TREND, PlayCause.CHOP, PlayCause.RULES])
def test_a_roll_session_is_blocked_whatever_the_play(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    cause: PlayCause,
) -> None:
    bt, result = _rolled(profiles, settings, history, tmp_path, cause, frozenset({LAST_DAY}))
    assert result.trades == []
    assert result.fired == 0
    decisions = bt.journal.decisions()
    assert decisions
    failed = {g["name"] for d in decisions for g in d["gates"] if not g["passed"]}
    assert "roll_session" in failed
