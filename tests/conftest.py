from pathlib import Path

import pytest

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
