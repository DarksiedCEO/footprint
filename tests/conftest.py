from pathlib import Path

import pytest

from footprint.core.config import (
    InstrumentProfile,
    RiskProfile,
    StrategyProfile,
    load_instrument,
    load_risk,
    load_strategy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def instrument_path() -> Path:
    return REPO_ROOT / "config" / "instruments" / "nq.yaml"


@pytest.fixture
def risk_path() -> Path:
    return REPO_ROOT / "config" / "risk.yaml"


@pytest.fixture
def strategy_path() -> Path:
    return REPO_ROOT / "config" / "strategy.yaml"


@pytest.fixture
def instrument(instrument_path: Path) -> InstrumentProfile:
    return load_instrument(instrument_path)


@pytest.fixture
def risk(risk_path: Path) -> RiskProfile:
    return load_risk(risk_path)


@pytest.fixture
def strategy(strategy_path: Path) -> StrategyProfile:
    return load_strategy(strategy_path)
