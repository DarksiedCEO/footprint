from datetime import UTC, datetime
from decimal import Decimal

import pytest

from footprint.core.config import InstrumentProfile, RiskProfile, load_instrument, load_risk
from footprint.core.models import Direction
from footprint.decision.risk.gate import AccountState, EntryRequest
from tests.conftest import REPO_ROOT

# Tuesday 2026-10-06 is US daylight time: Pacific = UTC-7.
PRIME_TS = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)  # Tue 06:30 PT
OVERNIGHT_TS = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)  # Wed 01:00 PT


@pytest.fixture
def risk() -> RiskProfile:
    return load_risk(REPO_ROOT / "config" / "risk.yaml")


@pytest.fixture
def instrument() -> InstrumentProfile:
    return load_instrument(REPO_ROOT / "config" / "instruments" / "nq.yaml")


@pytest.fixture
def clean_state(risk: RiskProfile) -> AccountState:
    """A fresh day: full bankroll, nothing traded, nothing open."""
    return AccountState(
        equity=risk.bankroll,
        peak_equity=risk.bankroll,
        realized_pnl_today=Decimal(0),
        realized_pnl_week=Decimal(0),
        stopouts_today=0,
        trades_today_prime=0,
        trades_today_overnight=0,
        position_open=False,
        kill_switch_latched=False,
    )


@pytest.fixture
def good_entry(risk: RiskProfile) -> EntryRequest:
    """A prime-session entry that breaks no rule on its own."""
    return EntryRequest(ts=PRIME_TS, direction=Direction.LONG, cost=risk.position_cap)
