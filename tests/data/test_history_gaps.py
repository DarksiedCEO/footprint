"""Q2: an exported history must have a bar for every minute Footprint could trade in.
Minutes the calendar calls CLOSED (the daily halt, weekends, holidays, after an early
close) are never gaps; a run of missing tradable minutes longer than the configured
allowance is. Hand-built bars on the real NQ calendar (config/instruments/nq.yaml)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.core.config import InstrumentProfile, load_instrument
from footprint.core.models import Bar
from footprint.data.gaps import first_gap
from tests.conftest import REPO_ROOT

MIN = timedelta(minutes=1)


@pytest.fixture(scope="module")
def nq() -> InstrumentProfile:
    return load_instrument(REPO_ROOT / "config" / "instruments" / "nq.yaml")


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)


def minutes(first: datetime, last: datetime, skip: frozenset[datetime] = frozenset()) -> list[Bar]:
    out = []
    m = first
    while m <= last:
        if m not in skip:
            out.append(Bar("NQ", m, Decimal(1), Decimal(2), Decimal(1), Decimal(1), 1))
        m += MIN
    return out


TUE = date(2026, 10, 6)  # PDT: prime 04:00-14:00 PT = 11:00-21:00 UTC; halt 21:01-21:59


def test_the_daily_halt_is_not_a_gap_but_its_end_minute_is_tradable(
    nq: InstrumentProfile,
) -> None:
    # The prime window includes its end minute (14:00 PT = 21:00 UTC), when CME has
    # already halted: one tradable minute with no bar. 21:01..21:59 are CLOSED.
    bars = minutes(at(TUE, 20, 0), at(TUE, 20, 59)) + minutes(at(TUE, 22, 0), at(TUE, 22, 30))
    start = at(TUE, 20, 0)
    assert first_gap(bars, start, nq, 1) is None
    gap = first_gap(bars, start, nq, 0)
    assert gap is not None
    assert "1 tradable minute(s) from 2026-10-06T21:00:00+00:00 have no bar" in gap


@pytest.mark.parametrize(("allowed", "is_gap"), [(5, False), (4, True), (6, False)])
def test_a_hole_inside_the_session_is_a_gap_only_past_the_allowance(
    nq: InstrumentProfile, allowed: int, is_gap: bool
) -> None:
    hole = frozenset(at(TUE, 15, 10) + i * MIN for i in range(5))
    bars = minutes(at(TUE, 15, 0), at(TUE, 15, 30), hole)
    gap = first_gap(bars, at(TUE, 15, 0), nq, allowed)
    if is_gap:
        assert gap is not None
        assert "5 tradable minute(s) from 2026-10-06T15:10:00+00:00 have no bar" in gap
        assert "at most 4" in gap
    else:
        assert gap is None


def test_two_short_holes_are_not_added_together(nq: InstrumentProfile) -> None:
    hole = frozenset({at(TUE, 15, 10), at(TUE, 15, 11), at(TUE, 15, 13), at(TUE, 15, 14)})
    bars = minutes(at(TUE, 15, 0), at(TUE, 15, 30), hole)
    assert first_gap(bars, at(TUE, 15, 0), nq, 2) is None
    assert first_gap(bars, at(TUE, 15, 0), nq, 1) is not None


def test_the_first_long_hole_is_named(nq: InstrumentProfile) -> None:
    hole = frozenset(
        [at(TUE, 15, 5) + i * MIN for i in range(3)] + [at(TUE, 15, 20) + i * MIN for i in range(4)]
    )
    bars = minutes(at(TUE, 15, 0), at(TUE, 15, 40), hole)
    gap = first_gap(bars, at(TUE, 15, 0), nq, 2)
    assert gap is not None
    assert "3 tradable minute(s) from 2026-10-06T15:05:00+00:00" in gap


def test_the_weekend_is_not_a_gap(nq: InstrumentProfile) -> None:
    fri, sun = date(2026, 10, 9), date(2026, 10, 11)
    # Friday's prime session ends 21:00 UTC; Sunday's overnight session opens 22:00 UTC
    bars = minutes(at(fri, 20, 0), at(fri, 21, 0)) + minutes(at(sun, 22, 0), at(sun, 22, 30))
    assert first_gap(bars, at(fri, 20, 0), nq, 0) is None


def test_a_missing_sunday_open_is_a_gap(nq: InstrumentProfile) -> None:
    fri, sun = date(2026, 10, 9), date(2026, 10, 11)
    bars = minutes(at(fri, 20, 0), at(fri, 21, 0)) + minutes(at(sun, 23, 0), at(sun, 23, 30))
    gap = first_gap(bars, at(fri, 20, 0), nq, 30)
    assert gap is not None
    assert "60 tradable minute(s) from 2026-10-11T22:00:00+00:00" in gap


def test_a_missing_start_of_the_window_is_a_gap(nq: InstrumentProfile) -> None:
    bars = minutes(at(TUE, 15, 10), at(TUE, 15, 30))
    assert first_gap(bars, at(TUE, 15, 10), nq, 0) is None
    gap = first_gap(bars, at(TUE, 15, 0), nq, 9)
    assert gap is not None
    assert "10 tradable minute(s) from 2026-10-06T15:00:00+00:00" in gap


def test_a_hole_on_a_holiday_is_not_a_gap(nq: InstrumentProfile) -> None:
    xmas = date(2026, 12, 25)  # in nq.yaml holidays: its prime window is CLOSED
    assert xmas in nq.holidays
    bars = [*minutes(at(xmas, 16, 0), at(xmas, 16, 0)), *minutes(at(xmas, 20, 0), at(xmas, 20, 0))]
    assert first_gap(bars, at(xmas, 16, 0), nq, 0) is None
    # the same hole on an ordinary Tuesday is 239 tradable minutes
    tue = [*minutes(at(TUE, 16, 0), at(TUE, 16, 0)), *minutes(at(TUE, 20, 0), at(TUE, 20, 0))]
    gap = first_gap(tue, at(TUE, 16, 0), nq, 0)
    assert gap is not None
    assert "239 tradable minute(s)" in gap


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (datetime(2025, 12, 31, 15, 0, tzinfo=UTC), datetime(2025, 12, 31, 15, 5, tzinfo=UTC)),
        (datetime(2027, 4, 1, 15, 0, tzinfo=UTC), datetime(2027, 4, 1, 15, 5, tzinfo=UTC)),
    ],
    ids=["before calendar_from", "after calendar_through"],
)
def test_bars_outside_the_maintained_calendar_are_refused(
    nq: InstrumentProfile, first: datetime, last: datetime
) -> None:
    gap = first_gap(minutes(first, last), first, nq, 1000)
    assert gap is not None
    assert "outside the maintained calendar" in gap


def test_the_calendar_edges_themselves_are_inside(nq: InstrumentProfile) -> None:
    first = datetime.combine(nq.calendar_from, datetime.min.time(), tzinfo=UTC)
    last = datetime.combine(nq.calendar_through, datetime.min.time(), tzinfo=UTC)
    assert first_gap(minutes(first, first + MIN), first, nq, 1000) is None
    assert first_gap(minutes(last, last + MIN), last, nq, 1000) is None


def test_no_bars_is_a_gap(nq: InstrumentProfile) -> None:
    assert first_gap([], at(TUE, 15, 0), nq, 1000) == "no bars at all"


def test_a_start_with_seconds_is_refused(nq: InstrumentProfile) -> None:
    bars = minutes(at(TUE, 15, 0), at(TUE, 15, 5))
    with pytest.raises(ValueError, match="whole minute"):
        first_gap(bars, at(TUE, 15, 0) + timedelta(seconds=30), nq, 0)


def test_a_start_before_the_calendar_is_refused_even_when_the_bars_are_inside(
    nq: InstrumentProfile,
) -> None:
    inside = datetime.combine(nq.calendar_from, datetime.min.time(), tzinfo=UTC) + 15 * 60 * MIN
    gap = first_gap(minutes(inside, inside + MIN), inside - 2 * 24 * 60 * MIN, nq, 100_000)
    assert gap is not None
    assert "outside the maintained calendar" in gap
