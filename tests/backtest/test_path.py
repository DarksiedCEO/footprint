"""The modelled path inside a minute: adverse-first against an open trade."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from footprint.backtest.path import path_price
from footprint.core.models import Bar, Direction

D = Decimal
BAR = Bar("NQ", datetime(2026, 9, 15, 14, 0, tzinfo=UTC), D(100), D(110), D(90), D(105), 1)


def lowest_and_highest_seconds(against: Direction | None) -> tuple[int, int]:
    prices = {s: path_price(BAR, s, against) for s in range(60)}
    return min(prices, key=lambda s: prices[s]), max(prices, key=lambda s: prices[s])


def test_against_a_long_the_low_comes_first() -> None:
    low_at, high_at = lowest_and_highest_seconds(Direction.LONG)
    assert low_at < high_at


def test_against_a_short_the_high_comes_first() -> None:
    low_at, high_at = lowest_and_highest_seconds(Direction.SHORT)
    assert high_at < low_at


def test_the_path_touches_every_price_of_the_bar() -> None:
    for against in (Direction.LONG, Direction.SHORT, None):
        prices = [path_price(BAR, s, against) for s in range(60)]
        assert prices[0] == BAR.open
        assert prices[-1] == BAR.close
        assert min(prices) == BAR.low
        assert max(prices) == BAR.high


def test_seconds_outside_the_minute_are_refused() -> None:
    with pytest.raises(ValueError, match="within the minute"):
        path_price(BAR, 60, None)
