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

from dataclasses import replace
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


LEVELS = [D(1), D("0.1"), D("0.5"), D(2)]


def _share(profiles: Profiles, pct: str) -> Profiles:
    """The decay exit's share of the open profit (DX-1) moved, to bracket the threshold."""
    theta = profiles.strategy.theta
    rule = replace(theta.decay_exit, max_pct_of_open_profit=D(pct))
    return replace(
        profiles, strategy=replace(profiles.strategy, theta=replace(theta, decay_exit=rule))
    )


# The ridden path at REF: the entry fills at 170.75 and the first decay-exit check sees
# an open profit of 5.00 with a modelled decay over the window of 28.53 (journaled).
# At the shipped 50% the limit is 2.50: out at 171.25. Measured, and the same at every
# price level, because the decay exit is quoted at the reference.
_FIRST_CHECK = ("DECAY_EXIT", "170.75", "171.25", "5.00", "CALL20000-20260916")


@pytest.mark.parametrize("k", LEVELS)
def test_with_the_decay_exit_on_the_dollars_are_pinned_at_every_level(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    k: Decimal,
) -> None:
    _, result = run(
        with_decay_exit(profiles), scaled(settings, REF), times(ridden(history), k), tmp_path
    )
    assert _decay(result) == [_FIRST_CHECK]
    assert result.final_equity == result.bankroll + D("5.00")
    [note] = [e for e in result.trades[0]["events"] if "decay exit:" in str(e)]
    assert "decay over the next 30 minutes 28.53" in str(note)
    assert "open profit 5.00 (limit 2.50)" in str(note)


@pytest.mark.parametrize("k", LEVELS)
def test_just_under_the_threshold_the_decay_exit_fires(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    k: Decimal,
) -> None:
    """560% of 5.00 is a limit of 28.00, just under the decay of 28.53: out."""
    p = _share(with_decay_exit(profiles), "560")
    _, result = run(p, scaled(settings, REF), times(ridden(history), k), tmp_path)
    assert _decay(result) == [_FIRST_CHECK]


@pytest.mark.parametrize("k", LEVELS)
def test_just_over_the_threshold_the_decay_exit_stays(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    k: Decimal,
) -> None:
    """580% of 5.00 is a limit of 29.00, just over the decay of 28.53: the trade is
    ridden and the trailed resting stop takes it out, at the same dollars at every level."""
    p = _share(with_decay_exit(profiles), "580")
    _, result = run(p, scaled(settings, REF), times(ridden(history), k), tmp_path)
    assert dollars(result) == [("STOP", "170.75", "200.25", "585.00", "CALL20000-20260916")]
    assert not [e for e in result.trades[0]["events"] if "decay exit:" in str(e)]


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


@pytest.mark.parametrize("cause", list(PlayCause))
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
