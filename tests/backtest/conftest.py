from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.settings import BacktestSettings, load_backtest
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, market

D = Decimal
MINUTE = timedelta(minutes=1)
TRADE_FROM = DECISION_TIME - MINUTE  # earlier bars are the readers' look-back only


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_play: run the backtest with the real play caller (no stand-in)"
    )


@pytest.fixture(autouse=True)
def trend_every_day(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backtest tests written before the play caller was wired check the crew, the
    console, the exits, the CLI and the readout on a 26-session synthetic market that
    the real regime reader can never label (it needs ~61 sessions). For them the
    backtest builds a stand-in play caller that says TREND every day, so they keep
    testing what they were written for. Tests marked `real_play` (and every test that
    hands Backtest its own `play`) run with the real one; tests/backtest/
    test_engine_play.py covers the real play caller in the backtest."""
    if request.node.get_closest_marker("real_play") is not None:
        return
    from footprint.backtest import engine
    from tests.decision.plays import open_play

    monkeypatch.setattr(engine, "PlayCaller", lambda *args, **kwargs: open_play())


@pytest.fixture(scope="session")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="session")
def settings() -> BacktestSettings:
    return load_backtest(REPO_ROOT / "config" / "backtest.yaml")


@pytest.fixture(scope="session")
def history() -> tuple[Bar, ...]:
    return tuple(market())  # fires a long entry at DECISION_TIME (07:01 PT)


ENTRY_DIP = D(8)  # reached by second ~25 even on the worst-case (high-first) path


def extend(
    history: Sequence[Bar], moves: Sequence[Decimal], *, dip: Decimal = ENTRY_DIP
) -> list[Bar]:
    """The scenario, then one bar per move from DECISION_TIME on. The first bar dips
    `dip` points below its open first, so the resting entry fills."""
    bars = list(history)
    price = bars[-1].close
    for i, move in enumerate(moves):
        o, c = price, price + move
        low = min(o, c) - (dip if i == 0 else D(1))
        bars.append(Bar("NQ", DECISION_TIME + MINUTE * i, o, max(o, c) + 1, low, c, 100))
        price = c
    return bars


def workdir(tmp_path: Path) -> Path:
    return tmp_path / "run"
