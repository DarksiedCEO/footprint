"""M3 (b): the backtest runs the live pipeline, so Felix's absolute spread cap applies to
modelled quotes too. The model's spread (option_model.spread_points, put on the option
tick) must stay under the cap, or the backtest would refuse every entry for a reason
that is not the market (and the goldens would change silently)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.agents.contract_picker import ContractChoice, ContractPicker
from footprint.backtest.options_model import OptionModel
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Direction

D = Decimal
T = datetime(2026, 9, 15, 14, 1, tzinfo=UTC)  # Tuesday 07:01 PT


def _model(profiles: Profiles, settings: BacktestSettings) -> OptionModel:
    return OptionModel(
        settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
    )


def test_every_modelled_spread_is_inside_the_points_cap(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    cap = profiles.execution.contract.max_spread_points
    tick = profiles.instrument.option_tick_size
    m = _model(profiles, settings)
    widest = D(0)
    for minutes in range(0, 6 * 60, 37):
        for offset in range(0, 100, 7):
            chain = m.chain(T + timedelta(minutes=minutes), D(23700) + D(offset) / 4)
            widest = max(widest, *(q.ask - q.bid for q in chain.quotes))
    assert widest <= settings.option_model.spread_points + tick  # on-tick rounding only
    assert widest < cap


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_the_modelled_contract_still_passes_felix_with_the_points_cap(
    profiles: Profiles, settings: BacktestSettings, direction: Direction
) -> None:
    chain = _model(profiles, settings).chain(T, D("23712"))
    choice = ContractPicker(profiles.execution, profiles.instrument).choose(chain, direction, T)
    assert isinstance(choice, ContractChoice), choice
    assert choice.quote.spread <= profiles.execution.contract.max_spread_points
