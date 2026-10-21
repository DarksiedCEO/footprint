from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from footprint.core.black76 import Right76, price
from footprint.research.pricing import choose, dollars, value, years_left
from footprint.research.settings import OptionSettings, ResearchSettings

T0 = datetime(2024, 3, 5, 8, 15, tzinfo=UTC)
OPT = OptionSettings(
    moneyness_points=0.0,
    days_to_expiry=1.0,
    days_per_year=365.0,
    rate=0.0,
    reference_price=None,
    strike_step=25.0,
    point_value=20.0,
)


def test_config_option_settings_are_read(research: ResearchSettings) -> None:
    o = research.option
    assert (o.moneyness_points, o.days_to_expiry, o.days_per_year, o.rate) == (0, 1, 365, 0)
    assert o.reference_price == 20000
    assert o.strike_step == 25  # backtest.yaml option_model.strike_step
    assert o.point_value == 20  # the instrument's point value
    assert research.round_trip_per_contract == 170


def test_direction_picks_call_or_put_never_anything_else() -> None:
    assert choose(1, 18010.0, T0, OPT).right is Right76.CALL
    assert choose(-1, 18010.0, T0, OPT).right is Right76.PUT
    for bad in (0, 2, -2):
        with pytest.raises(ValueError, match="direction"):
            choose(bad, 18010.0, T0, OPT)


@pytest.mark.parametrize(
    ("direction", "moneyness", "entry", "strike"),
    [
        (1, 0.0, 18010.0, 18000.0),  # at the money: nearest step
        (1, 0.0, 18013.0, 18025.0),
        (-1, 0.0, 18013.0, 18025.0),
        (1, 50.0, 18010.0, 17950.0),  # in the money call: below the price
        (-1, 50.0, 18010.0, 18050.0),  # in the money put: above the price
        (1, -50.0, 18010.0, 18050.0),  # out of the money call
        (-1, -50.0, 18010.0, 17950.0),
        (1, 0.0, 18012.5, 18000.0),  # a tie goes to the even multiple of the step
        (1, 0.0, 18037.5, 18050.0),
    ],
)
def test_strike_from_moneyness_rounded_to_the_step(
    direction: int, moneyness: float, entry: float, strike: float
) -> None:
    c = choose(direction, entry, T0, replace(OPT, moneyness_points=moneyness))
    assert c.strike == strike


def test_strike_step_comes_from_settings() -> None:
    c = choose(1, 18013.0, T0, replace(OPT, strike_step=5.0))
    assert c.strike == 18015.0


def test_years_left_counts_calendar_time_from_entry() -> None:
    c = choose(1, 18000.0, T0, replace(OPT, days_to_expiry=7.0))
    assert c.expiry == T0 + timedelta(days=7)
    assert years_left(c, T0, OPT) == pytest.approx(7 / 365)
    assert years_left(c, T0 + timedelta(hours=12), OPT) == pytest.approx(6.5 / 365)
    assert years_left(c, T0 + timedelta(days=7), OPT) == 0.0
    assert years_left(c, T0, replace(OPT, days_per_year=360.0)) == pytest.approx(7 / 360)
    half = choose(1, 18000.0, T0, replace(OPT, days_to_expiry=0.5))
    assert half.expiry == T0 + timedelta(hours=12)
    with pytest.raises(ValueError, match="expiry"):
        years_left(c, T0 + timedelta(days=7, seconds=1), OPT)


def test_value_is_black76_with_the_settings() -> None:
    opt = replace(OPT, rate=0.05)
    c = choose(-1, 18010.0, T0, opt)
    at = T0 + timedelta(hours=2)
    expected = price(
        Right76.PUT, forward=17990.0, strike=18000.0, years=(1 - 2 / 24) / 365, vol=0.25, rate=0.05
    )
    assert value(c, 17990.0, at, 0.25, opt) == pytest.approx(expected, rel=1e-12)


def test_without_reference_price_nothing_is_rescaled() -> None:
    c = choose(1, 9000.0, T0, OPT)
    assert c.scale == 1.0
    assert value(c, 9000.0, T0, 0.2, OPT) == pytest.approx(
        price(Right76.CALL, forward=9000.0, strike=9000.0, years=1 / 365, vol=0.2, rate=0.0)
    )


def test_reference_price_rescales_forward_and_strike_by_the_entry_ratio() -> None:
    opt = replace(OPT, reference_price=20000.0)
    c = choose(1, 5010.0, T0, opt)  # a 2015-ish price level
    assert c.strike == 5000.0
    assert c.scale == pytest.approx(20000.0 / 5010.0)
    got = value(c, 5020.0, T0, 0.2, opt)
    s = 20000.0 / 5010.0
    assert got == pytest.approx(
        price(Right76.CALL, forward=5020.0 * s, strike=5000.0 * s, years=1 / 365, vol=0.2, rate=0.0)
    )


@pytest.mark.parametrize("right", [Right76.CALL, Right76.PUT])
@pytest.mark.parametrize("scale", [0.25, 3.7, 4.0])
def test_black76_is_homogeneous_of_degree_one(right: Right76, scale: float) -> None:
    """Why the rescaling is exact: scaling forward and strike scales the price."""
    base = price(right, forward=5020.0, strike=5000.0, years=2 / 365, vol=0.3, rate=0.01)
    scaled = price(
        right, forward=5020.0 * scale, strike=5000.0 * scale, years=2 / 365, vol=0.3, rate=0.01
    )
    assert scaled == pytest.approx(base * scale, rel=1e-10)


def test_dollars_is_value_change_times_point_value_times_contracts_less_costs() -> None:
    assert dollars(100.0, 150.0, 1, 20.0, 170.0) == pytest.approx(1000 - 170)
    assert dollars(100.0, 150.0, 2, 20.0, 170.0) == pytest.approx(2 * 1000 - 2 * 170)
    assert dollars(100.0, 80.0, 1, 20.0, 0.0) == pytest.approx(-400)
    assert dollars(100.0, 100.0, 3, 50.0, 10.0) == pytest.approx(-30)
