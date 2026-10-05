"""The series caches are exact: for any bars, a tuple (cached) and a list (never
cached) give identical answers, on the first call and on every repeat."""

import random
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from footprint.agents._series import same_time_indices
from footprint.core.config import Session
from footprint.core.models import Bar
from footprint.core.series import require_series
from footprint.data.aggregate import resample, session_days

START = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # spans the US DST change (Mar 8)
TZ = "America/Los_Angeles"


def random_bars(seed: int, count: int) -> list[Bar]:
    rng = random.Random(seed)
    bars, ts, price = [], START, Decimal(20000)
    for _ in range(count):
        ts += timedelta(minutes=rng.choice([1, 1, 1, 1, 2, 7, 60 * 13]))  # gaps, nights
        move = Decimal(rng.randint(-8, 8))
        high = price + abs(move) + 1
        low = price - abs(move) - 1
        close = price + move
        bars.append(
            Bar("NQ", ts, price, max(high, close), min(low, close), close, rng.randint(0, 5000))
        )
        price = close
    return bars


@pytest.mark.parametrize("seed", range(6))
def test_same_time_indices_cached_equals_uncached(seed: int) -> None:
    bars = random_bars(seed, 6000)
    as_tuple = tuple(bars)
    rng = random.Random(seed)
    for _ in range(60):
        index = rng.randrange(len(bars))
        lookback = rng.choice([1, 3, 20])
        expected = same_time_indices(bars, index, lookback, TZ)
        assert same_time_indices(as_tuple, index, lookback, TZ) == expected
        assert same_time_indices(as_tuple, index, lookback, TZ) == expected  # cache hit


@pytest.mark.parametrize("seed", range(4))
def test_resample_and_session_days_cached_equal_uncached(seed: int) -> None:
    bars = random_bars(seed, 4000)
    as_tuple = tuple(bars)
    session = Session(start=time(6, 30), end=time(13, 0), timezone=TZ)
    for as_of in (bars[2000].ts, bars[-1].ts + timedelta(minutes=1), bars[-1].ts):
        for minutes in (15, 60):
            expected = resample(bars, minutes, as_of)
            assert resample(as_tuple, minutes, as_of) == expected
            assert resample(as_tuple, minutes, as_of) == expected
        expected_days = session_days(bars, session, as_of)
        assert session_days(as_tuple, session, as_of) == expected_days
        assert session_days(as_tuple, session, as_of) == expected_days


def test_a_cached_resample_cannot_be_changed_by_its_caller() -> None:
    bars = tuple(random_bars(1, 500))
    as_of = bars[-1].ts + timedelta(minutes=1)
    first = resample(bars, 15, as_of)
    first.clear()  # a caller mutates the list it got
    assert resample(bars, 15, as_of) != []


@pytest.mark.parametrize("make", [list, tuple])
def test_validation_errors_are_identical_for_tuples_and_lists(make: type) -> None:
    good = random_bars(2, 50)
    backwards = [*good[:10], good[5], *good[11:]]
    with pytest.raises(ValueError, match="strictly increasing"):
        require_series(make(backwards), 1, "bars")
    require_series(make(good), 1, "bars")  # valid either way
    require_series(make(good), 1, "bars")  # and again (cached for a tuple)


def test_one_tuple_in_two_time_zones_is_cached_per_zone() -> None:
    bars = random_bars(9, 3000)
    as_tuple = tuple(bars)
    for zone in (TZ, "Europe/London", TZ):
        for index in (2999, 1500, 700):
            assert same_time_indices(as_tuple, index, 5, zone) == same_time_indices(
                bars, index, 5, zone
            )


def test_a_tuple_checked_without_a_timeframe_is_still_checked_with_one() -> None:
    two_minute = tuple(random_bars(3, 10)[::2])
    gappy = tuple(
        Bar(b.symbol, START + timedelta(minutes=2 * i), b.open, b.high, b.low, b.close, b.volume)
        for i, b in enumerate(two_minute)
    )
    require_series(gappy, None, "bars")  # fine without a timeframe
    with pytest.raises(ValueError, match="not a multiple of 15"):
        require_series(gappy, 15, "bars")


def test_a_list_is_rechecked_even_after_it_passed() -> None:
    bars = random_bars(4, 20)
    require_series(bars, 1, "bars")
    bars[5], bars[6] = bars[6], bars[5]  # mutated in place: now out of order
    with pytest.raises(ValueError, match="strictly increasing"):
        require_series(bars, 1, "bars")
