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
