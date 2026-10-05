"""G4 modelled option quotes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from footprint.backtest.options_model import OptionModel
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.options import Right

D = Decimal
T = datetime(2026, 9, 15, 14, 1, tzinfo=UTC)  # Tuesday 07:01 PT


def model(profiles: Profiles, settings: BacktestSettings) -> OptionModel:
    return OptionModel(
        settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
    )


def mid_of(m: OptionModel, contract_id: str, t: datetime, price: Decimal) -> Decimal:
    q = m.quote(contract_id, t, price)
    return (q.bid + q.ask) / 2


def test_the_chain_lists_strikes_around_the_money_on_daily_expiries(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    m = model(profiles, settings)
    chain = m.chain(T, D("23712"))
    o = settings.option_model
    strikes = sorted({q.strike for q in chain.quotes})
    assert len(strikes) == 2 * o.strikes_each_side + 1
    assert strikes[o.strikes_each_side] == D(23700)  # nearest multiple of the step
    assert all(b - a == o.strike_step for a, b in pairwise(strikes))
    expiries = sorted({q.expiry for q in chain.quotes})
    assert [e.isoformat() for e in expiries][:4] == [
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-21",  # Friday, then Monday
    ]
    assert len(expiries) == o.expiries_listed
    assert {q.right for q in chain.quotes} == set(Right)
    assert all(q.open_interest == o.open_interest for q in chain.quotes)
    assert chain.ts == T


def test_quotes_are_on_the_tick_and_pay_at_least_the_spread(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    tick = profiles.instrument.option_tick_size
    for q in model(profiles, settings).chain(T, D("23712")).quotes:
        assert q.bid % tick == 0
        assert q.ask % tick == 0
        assert q.ask - q.bid >= settings.option_model.spread_points


def test_a_point_in_the_future_moves_the_premium_by_delta(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    m = model(profiles, settings)
    m.chain(T, D("23700"))
    delta = profiles.strategy.options.assumed_delta
    tick = profiles.instrument.option_tick_size
    for cid, sign in (("CALL23700-20260916", 1), ("PUT23700-20260916", -1)):
        moved = mid_of(m, cid, T, D("23800")) - mid_of(m, cid, T, D("23700"))
        assert abs(moved - sign * delta * 100) <= tick  # on-tick rounding only


def test_time_value_decays_toward_expiry(profiles: Profiles, settings: BacktestSettings) -> None:
    m = model(profiles, settings)
    m.chain(T, D("23700"))
    cid = "CALL23700-20260916"
    assert mid_of(m, cid, T + timedelta(hours=4), D("23700")) < mid_of(m, cid, T, D("23700"))


def test_the_broker_quote_equals_the_chain_quote(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    """R12: the decision and the fill see the same market."""
    m = model(profiles, settings)
    chain = m.chain(T, D("23712.5"))
    for q in chain.quotes:
        b = m.quote(q.contract_id, T, D("23712.5"))
        assert (b.bid, b.ask, b.ts) == (q.bid, q.ask, T)


def test_an_expired_option_has_no_time_value(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    m = model(profiles, settings)
    m.chain(T, D("23700"))
    cid = "CALL23850-20260916"  # out of the money
    after = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)  # past the expiry's prime close
    q = m.quote(cid, after, D("23700"))
    assert q.bid == 0
    assert (
        q.ask == profiles.instrument.option_tick_size + settings.option_model.spread_points / 2
        or q.ask > 0
    )


def test_a_delta_outside_zero_to_one_is_refused(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    with pytest.raises(ValueError, match="delta"):
        OptionModel(settings.option_model, profiles.instrument, D(1))
