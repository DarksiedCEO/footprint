"""CAP-1 review M1: in the backtest, the position cap decides Friday entries.

Felix takes the nearest expiry inside 1-3 days, so Monday to Thursday he buys a 1-day
option and only on a Friday (Monday expiry, 3 days) a pricier one. On the modelled
chain (config/backtest.yaml) a Friday at-the-money option costs more than the old
$5,000 cap and less than the shipped $6,000 (CAP-1 amended by M2 (b), Andre
2026-10-24; this test's Friday costs $5,567.50), so the old cap refused every Friday
entry and the shipped cap takes it. A Monday-Thursday entry (a 1-day option, even on a
Monday when 2- and 3-day expiries are also listed) fits under both.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.contract_picker import ContractChoice, ContractPicker
from footprint.backtest.options_model import OptionModel
from footprint.backtest.settings import BacktestSettings
from footprint.core.calendar import SessionKind
from footprint.core.config import Profiles
from footprint.core.models import Direction
from footprint.decision.sizing import EntryPlan, NoSize, size

D = Decimal
PT = ZoneInfo("America/Los_Angeles")
OLD_CAP = D(5000)  # the cap before CAP-1
SHIPPED_CAP = D(6000)  # CAP-1 amended by M2 (b), Andre 2026-10-24
UNDERLYING = D("24012.5")
MONDAY = datetime(2026, 9, 14, 7, 0, tzinfo=PT).astimezone(UTC)  # 1-, 2- and 3-day expiries
THURSDAY = datetime(2026, 9, 17, 7, 0, tzinfo=PT).astimezone(UTC)
FRIDAY = datetime(2026, 9, 18, 7, 0, tzinfo=PT).astimezone(UTC)


def _choice(
    profiles: Profiles, settings: BacktestSettings, now: datetime, direction: Direction
) -> ContractChoice:
    model = OptionModel(
        settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
    )
    felix = ContractPicker(profiles.execution, profiles.instrument)
    choice = felix.choose(model.chain(now, UNDERLYING), direction, now)
    assert isinstance(choice, ContractChoice), choice
    return choice


def _size(profiles: Profiles, choice: ContractChoice, cap: Decimal) -> EntryPlan | NoSize:
    risk = replace(profiles.risk, position_cap=cap)
    return size(choice, SessionKind.PRIME, risk, profiles.instrument, profiles.execution)


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_a_friday_entry_is_refused_at_5000_and_taken_at_the_shipped_6000(
    profiles: Profiles, settings: BacktestSettings, direction: Direction
) -> None:
    choice = _choice(profiles, settings, FRIDAY, direction)
    assert choice.dte == 3  # Monday's expiry
    old = _size(profiles, choice, OLD_CAP)
    assert isinstance(old, NoSize)
    assert "over the 5000 cap" in old.reason
    assert profiles.risk.position_cap == SHIPPED_CAP
    new = _size(profiles, choice, profiles.risk.position_cap)  # the shipped cap
    assert isinstance(new, EntryPlan)
    assert new.contracts == 1
    assert OLD_CAP < new.cost <= SHIPPED_CAP


@pytest.mark.parametrize("now", [MONDAY, THURSDAY], ids=["monday", "thursday"])
@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_a_monday_to_thursday_entry_is_a_1_day_option_under_both_caps(
    profiles: Profiles, settings: BacktestSettings, direction: Direction, now: datetime
) -> None:
    choice = _choice(profiles, settings, now, direction)
    assert choice.dte == 1
    old = _size(profiles, choice, OLD_CAP)
    new = _size(profiles, choice, profiles.risk.position_cap)
    assert isinstance(old, EntryPlan)
    assert new == old
