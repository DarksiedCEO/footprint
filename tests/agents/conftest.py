from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.core.config import (
    InstrumentProfile,
    StrategyProfile,
    load_instrument,
    load_strategy,
)
from footprint.core.models import Bar
from tests.conftest import REPO_ROOT


def bar(
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int = 100,
    symbol: str = "NQ",
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def series(start: datetime, minutes: int, mids: list[float]) -> list[Bar]:
    """Bars `minutes` apart; each bar spans mid +/- 1 and closes at mid."""
    return [
        bar(start + timedelta(minutes=minutes * i), m, m + 1, m - 1, m) for i, m in enumerate(mids)
    ]


# 06:30 Pacific daylight time on 2026-09-01.
DAY0_0630 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


@pytest.fixture
def strategy() -> StrategyProfile:
    return load_strategy(REPO_ROOT / "config" / "strategy.yaml")


@pytest.fixture
def instrument() -> InstrumentProfile:
    return load_instrument(REPO_ROOT / "config" / "instruments" / "nq.yaml")
