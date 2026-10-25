"""The exchange calendar back to 2010-06-01 (backtest-history-v1, BH-1 .. BH-6).

The shipped calendar is complete from `calendar_from` (2010-06-01) to
`calendar_through`: every standard closure and early close is listed, the loader's
completeness check runs over the whole window, and two rule refinements make that
check exact for history:
- `first_year`: a rule that only exists from a year on (Juneteenth, first observed by
  the stock market and CME equity in 2022);
- `weekdays`: a rule that only counts on some weekdays (the stock market's July 3 half
  day, which exists only when July 3 is Monday to Thursday; on a Friday it is the
  observed Independence Day itself).
`early_close_periods` ends the prime session at one time on every trading day of a
span: CME's 15:15-15:30 CT equity maintenance halt (13:15 PT) inside the prime session,
until it was removed after 2021-06-25. Seen in the 1-minute NQ data: before 2021-06-28
no bar starts in 13:16-13:28 PT on any day; 80 days have a single bar at the halt's
edges (76 at 13:15, 4 at 13:29); from 2021-06-28 a bar starts in every minute of it.

The listed dates were checked against the NQ 1-minute data (2010-06-06 to 2024-09-27):
see docs/decisions-pending.md, "BH-* backtest on history". The pins below are spot
checks of what that data showed, not a second copy of the lists.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from footprint.core.calendar import (
    SessionKind,
    is_tradable_day,
    previous_trading_day,
    prime_close,
    session_at,
)
from footprint.core.config import ConfigError, InstrumentProfile, load_instrument
from footprint.core.expiry import expiry_moment
from footprint.core.holiday_rules import HolidayRule, missing_days, rule_date
from tests.test_config import rewrite

PT = ZoneInfo("America/Los_Angeles")
FROM = date(2010, 6, 1)
HALT_LAST = date(2021, 6, 25)  # the last day of the 13:15 PT maintenance halt
HALT = time(13, 15)


def pt(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), PT).astimezone(UTC)


def gregorian_easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher): an independent check of the
    listed Easter Sundays."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * m_) // 451
    month = (h + m_ - 7 * m + 114) // 31
    return date(year, month, (h + m_ - 7 * m + 114) % 31 + 1)


def _yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    return data


# --- the shipped window -------------------------------------------------------------


def test_the_calendar_starts_on_2010_06_01(instrument: InstrumentProfile) -> None:
    assert instrument.calendar_from == FROM
    assert instrument.calendar_through == date(2027, 3, 31)
    assert is_tradable_day(date(2010, 6, 1), instrument)  # a Tuesday
    assert not is_tradable_day(date(2010, 5, 28), instrument)  # before it: closed


def test_the_whole_window_is_complete(instrument: InstrumentProfile) -> None:
    closed = {*instrument.holidays, *instrument.no_trade_days}
    assert (
        missing_days(
            instrument.holiday_rules,
            instrument.easter_sundays,
            closed,
            set(instrument.early_closes),
            instrument.calendar_from,
            instrument.calendar_through,
        )
        == []
    )
    early_rules = [r for r in instrument.holiday_rules if r.kind == "early_close"]
    assert (
        missing_days(
            early_rules,
            instrument.easter_sundays,
            set(instrument.holidays),
            set(instrument.option_expiry.early_close_dates),
            instrument.calendar_from,
            instrument.calendar_through,
        )
        == []
    )


def test_every_easter_sunday_is_listed_and_right(instrument: InstrumentProfile) -> None:
    years = range(instrument.calendar_from.year, instrument.calendar_through.year + 1)
    assert instrument.easter_sundays == frozenset(gregorian_easter(y) for y in years)


@pytest.mark.parametrize(
    "day",
    [
        date(2010, 12, 24),  # Christmas Day observed (Saturday 25th)
        date(2012, 1, 2),  # New Year's Day observed (Sunday 1st)
        date(2012, 4, 6),  # Good Friday (the data has 04:01-06:14 only: closed before the open)
        date(2012, 10, 29),  # Hurricane Sandy
        date(2012, 10, 30),  # Hurricane Sandy
        date(2013, 1, 21),  # MLK: no bar in the prime session in the data
        date(2014, 2, 17),  # Presidents Day: no bar in the prime session in the data
        date(2018, 12, 5),  # national day of mourning, G.H.W. Bush (04:00-06:29 only)
        date(2021, 12, 24),  # Christmas Day observed (Saturday 25th)
        date(2025, 1, 9),  # national day of mourning, Carter (UNVERIFIED: after the data)
        date(2025, 4, 18),  # Good Friday
    ],
)
def test_full_closures(instrument: InstrumentProfile, day: date) -> None:
    assert day in instrument.holidays
    assert session_at(pt(day, 7, 0), instrument) is SessionKind.CLOSED
    assert session_at(pt(day - timedelta(days=1), 18, 0), instrument) is SessionKind.CLOSED


@pytest.mark.parametrize(
    ("day", "close"),
    [
        (date(2010, 7, 5), time(8, 30)),  # US holiday, 2010-2012: 10:30 CT halt in the data
        (date(2012, 9, 3), time(8, 30)),
        (date(2014, 5, 26), time(10, 0)),  # from 2014: the 12:00 CT halt
        (date(2022, 6, 20), time(10, 0)),  # Juneteenth, first year
        (date(2024, 9, 2), time(10, 0)),
        (date(2025, 9, 1), time(10, 0)),  # UNVERIFIED (after the data)
    ],
)
def test_us_holiday_sessions_are_no_trade_days_with_their_halt(
    instrument: InstrumentProfile, day: date, close: time
) -> None:
    assert day in instrument.no_trade_days
    assert instrument.early_closes[day] == close
    assert not is_tradable_day(day, instrument)


@pytest.mark.parametrize(
    ("day", "close"),
    [
        (date(2010, 11, 26), time(10, 15)),  # day after Thanksgiving
        (date(2012, 7, 3), time(10, 15)),  # July 3 (the data reopens at 13:30: ignored)
        (date(2015, 11, 27), time(10, 15)),
        (date(2019, 12, 24), time(10, 15)),  # Christmas Eve
        (date(2024, 7, 3), time(10, 15)),
        (date(2024, 12, 24), time(10, 0)),  # after the data: the shipped earlier close
        (date(2025, 7, 3), time(10, 0)),
    ],
)
def test_stock_half_days_close_the_prime_session_early(
    instrument: InstrumentProfile, day: date, close: time
) -> None:
    assert instrument.early_closes[day] == close
    assert day in instrument.option_expiry.early_close_dates
    assert is_tradable_day(day, instrument)
    assert prime_close(day, instrument) == close
    assert session_at(pt(day, close.hour, close.minute), instrument) is SessionKind.PRIME
    after = datetime.combine(day, close, PT) + timedelta(minutes=1)
    assert session_at(after.astimezone(UTC), instrument) is SessionKind.CLOSED
    # the options stop at noon CT (10:00 PT) that day, never after the futures halt
    assert expiry_moment(day, instrument) == pt(day, 10, 0)


def test_juneteenth_counts_only_from_its_first_year(instrument: InstrumentProfile) -> None:
    rule = next(r for r in instrument.holiday_rules if r.name == "Juneteenth")
    assert rule.first_year == 2022
    assert rule_date(rule, 2021, {}) is None
    assert rule_date(rule, 2022, {}) == date(2022, 6, 20)  # Sunday -> Monday
    assert is_tradable_day(date(2021, 6, 18), instrument)
    assert is_tradable_day(date(2019, 6, 19), instrument)


def test_the_july_3_half_day_rule_counts_monday_to_thursday(
    instrument: InstrumentProfile,
) -> None:
    rule = next(r for r in instrument.holiday_rules if r.name == "Independence Day eve")
    assert rule.kind == "early_close"
    assert rule_date(rule, 2017, {}) == date(2017, 7, 3)  # Monday
    assert rule_date(rule, 2014, {}) == date(2014, 7, 3)  # Thursday
    assert rule_date(rule, 2015, {}) is None  # Friday: the observed holiday itself
    assert rule_date(rule, 2026, {}) is None  # Friday
    assert rule_date(rule, 2016, {}) is None  # Sunday


def test_a_weekday_filter_and_a_first_year_apply_to_every_rule_shape() -> None:
    easters = {2024: date(2024, 3, 31), 2025: date(2025, 4, 20)}
    by_easter = HolidayRule("E", days_from_easter=-2, first_year=2025)
    assert rule_date(by_easter, 2024, easters) is None
    assert rule_date(by_easter, 2025, easters) == date(2025, 4, 18)
    by_nth = HolidayRule("N", month=1, weekday=0, nth=3, weekdays=frozenset({1}))
    assert rule_date(by_nth, 2026, {}) is None  # a Monday, only Tuesdays count
    friday = frozenset({4})
    friday_only = HolidayRule("F", month=11, weekday=3, nth=4, days_after=1, weekdays=friday)
    assert rule_date(friday_only, 2026, {}) == date(2026, 11, 27)  # after days_after
    fixed = HolidayRule("D", month=7, day=4, observed="weekend", weekdays=friday)
    assert rule_date(fixed, 2026, {}) == date(2026, 7, 3)  # the OBSERVED day is filtered
    assert rule_date(fixed, 2024, {}) is None  # a Thursday
    fixed_first = HolidayRule("Y", month=7, day=4, first_year=2025)
    assert rule_date(fixed_first, 2024, {}) is None
    assert rule_date(fixed_first, 2025, {}) == date(2025, 7, 4)


def test_the_maintenance_halt_ends_the_prime_session_at_13_15(
    instrument: InstrumentProfile,
) -> None:
    day = date(2015, 3, 10)  # an ordinary Tuesday
    assert prime_close(day, instrument) == HALT
    assert session_at(pt(day, 13, 15), instrument) is SessionKind.PRIME
    assert session_at(pt(day, 13, 16), instrument) is SessionKind.CLOSED
    assert session_at(pt(day, 15, 0), instrument) is SessionKind.OVERNIGHT
    assert prime_close(HALT_LAST, instrument) == HALT
    after = date(2021, 6, 28)
    assert prime_close(after, instrument) == time(14, 0)
    assert session_at(pt(after, 13, 30), instrument) is SessionKind.PRIME


def test_the_halt_covers_every_trading_day_of_its_span_and_never_moves_a_close_later(
    instrument: InstrumentProfile,
) -> None:
    day = FROM
    while day <= HALT_LAST:
        if day.weekday() < 5 and day not in instrument.holidays:
            assert instrument.early_closes[day] <= HALT, day
        else:
            assert day not in instrument.early_closes, day
        day += timedelta(days=1)
    assert instrument.early_closes[date(2010, 7, 5)] == time(8, 30)  # the earlier one kept
    assert date(2021, 6, 28) not in instrument.early_closes


def test_previous_trading_day_skips_history_closures(instrument: InstrumentProfile) -> None:
    assert previous_trading_day(date(2012, 10, 31), instrument) == date(2012, 10, 26)
    assert previous_trading_day(date(2013, 1, 22), instrument) == date(2013, 1, 18)
    assert previous_trading_day(date(2010, 7, 6), instrument) == date(2010, 7, 2)


# --- the loader ---------------------------------------------------------------------


def test_a_missing_history_day_is_refused(instrument_path: Path, tmp_path: Path) -> None:
    data = _yaml(instrument_path)
    data["no_trade_days"] = [d for d in data["no_trade_days"] if d != date(2016, 9, 5)]
    early = dict(data["early_closes"])
    early.pop(date(2016, 9, 5))
    data["early_closes"] = early
    with pytest.raises(ConfigError, match=r"2016-09-05 \(Labor Day\)"):
        load_instrument(rewrite(instrument_path, tmp_path, **data))


def test_a_missing_july_3_half_day_is_refused(instrument_path: Path, tmp_path: Path) -> None:
    data = _yaml(instrument_path)
    early = dict(data["early_closes"])
    early.pop(date(2019, 7, 3))
    with pytest.raises(ConfigError, match=r"2019-07-03 \(Independence Day eve, early close\)"):
        load_instrument(rewrite(instrument_path, tmp_path, early_closes=early))
    expiry = dict(data["option_expiry"])
    expiry["early_close_dates"] = [d for d in expiry["early_close_dates"] if d != date(2019, 7, 3)]
    with pytest.raises(ConfigError, match=r"early_close_dates lacks 2019-07-03"):
        load_instrument(rewrite(instrument_path, tmp_path, option_expiry=expiry))


def test_a_missing_juneteenth_is_refused_from_its_first_year(
    instrument_path: Path, tmp_path: Path
) -> None:
    data = _yaml(instrument_path)
    data["no_trade_days"] = [d for d in data["no_trade_days"] if d != date(2023, 6, 19)]
    early = dict(data["early_closes"])
    early.pop(date(2023, 6, 19))
    data["early_closes"] = early
    with pytest.raises(ConfigError, match=r"2023-06-19 \(Juneteenth\)"):
        load_instrument(rewrite(instrument_path, tmp_path, **data))


def test_a_missing_halt_period_is_refused_by_its_key(instrument_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="early_close_periods"):
        load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=...))


def test_no_halt_period_gives_the_normal_close(instrument_path: Path, tmp_path: Path) -> None:
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[]))
    assert profile.early_close_periods == ()
    assert prime_close(date(2015, 3, 10), profile) == time(14, 0)
    assert prime_close(date(2010, 7, 5), profile) == time(8, 30)


def _period(**changes: Any) -> dict[str, Any]:
    return {"from": date(2015, 3, 2), "through": date(2015, 3, 6), "close": "12:00"} | changes


def test_a_halt_period_applies_to_its_own_days_only(instrument_path: Path, tmp_path: Path) -> None:
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[_period()]))
    assert prime_close(date(2015, 3, 2), profile) == time(12, 0)  # first day: inclusive
    assert prime_close(date(2015, 3, 6), profile) == time(12, 0)  # last day: inclusive
    assert prime_close(date(2015, 3, 9), profile) == time(14, 0)
    assert prime_close(date(2015, 2, 27), profile) == time(14, 0)
    assert date(2015, 3, 7) not in profile.early_closes  # a Saturday is never listed


def test_a_halt_period_never_overrides_an_earlier_close_or_a_holiday(
    instrument_path: Path, tmp_path: Path
) -> None:
    period = _period(**{"from": date(2014, 11, 24), "through": date(2014, 11, 28)})
    period["close"] = "10:05"
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[period]))
    assert prime_close(date(2014, 11, 26), profile) == time(10, 5)
    assert prime_close(date(2014, 11, 27), profile) == time(10, 0)  # Thanksgiving's halt
    period["close"] = "10:20"
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[period]))
    assert prime_close(date(2014, 11, 28), profile) == time(10, 15)  # the half day's


def test_a_period_may_be_one_day_and_may_span_the_whole_window(
    instrument_path: Path, tmp_path: Path
) -> None:
    one = _period(through=date(2015, 3, 2))
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[one]))
    assert prime_close(date(2015, 3, 2), profile) == time(12, 0)
    assert prime_close(date(2015, 3, 3), profile) == time(14, 0)
    whole = _period(**{"from": FROM, "through": date(2027, 3, 31)})
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[whole]))
    assert prime_close(FROM, profile) == time(12, 0)
    assert prime_close(date(2027, 3, 31), profile) == time(12, 0)


def test_two_periods_take_the_earlier_close(instrument_path: Path, tmp_path: Path) -> None:
    periods = [_period(), _period(close="11:00")]
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=periods))
    assert prime_close(date(2015, 3, 3), profile) == time(11, 0)
    periods.reverse()
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=periods))
    assert prime_close(date(2015, 3, 3), profile) == time(11, 0)


def test_a_period_over_a_holiday_skips_it(instrument_path: Path, tmp_path: Path) -> None:
    period = _period(**{"from": date(2015, 12, 21), "through": date(2015, 12, 31)})
    profile = load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=[period]))
    assert date(2015, 12, 25) in profile.holidays
    assert date(2015, 12, 25) not in profile.early_closes
    assert prime_close(date(2015, 12, 24), profile) == time(10, 15)
    assert prime_close(date(2015, 12, 28), profile) == time(12, 0)


@pytest.mark.parametrize(
    ("periods", "message"),
    [
        ("not a list", "early_close_periods must be a list"),
        ([{"from": date(2015, 3, 2), "through": date(2015, 3, 6)}], "from, through and close"),
        ([_period(extra=1)], "from, through and close"),
        (["2015-03-02"], "from, through and close"),
        ([_period(**{"from": "2015-03-02"})], "dates"),
        ([_period(through=date(2015, 3, 1))], "from is after through"),
        ([_period(close="12:00:30")], "HH:MM"),
        ([_period(close="12:00+00:00")], "HH:MM"),  # RV7: a time zone would be dropped
        ([_period(**{"from": datetime(2015, 3, 2, 9, 0, tzinfo=UTC)})], "dates"),  # RV6
        ([_period(through=datetime(2015, 3, 6, 9, 0, tzinfo=UTC))], "dates"),  # RV6
        ([_period(close="ten")], "HH:MM"),
        ([_period(close=1200)], "HH:MM"),
        ([_period(close="06:30")], "after the cash open"),
        ([_period(close="14:00")], "before the normal prime close"),
        ([_period(**{"from": date(2010, 5, 31)})], "before calendar_from"),
        ([_period(through=date(2027, 4, 1))], "after calendar_through"),
        # only a weekend outside the window: no day of it is listed, the period itself is
        # still out of the window
        ([_period(**{"from": date(2010, 5, 29), "through": date(2010, 5, 30)})], "calendar_from"),
        ([_period(**{"from": date(2027, 4, 3), "through": date(2027, 4, 4)})], "calendar_through"),
    ],
)
def test_bad_halt_periods_are_refused(
    instrument_path: Path, tmp_path: Path, periods: object, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_instrument(rewrite(instrument_path, tmp_path, early_close_periods=periods))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"first_year": "2022"}, "first_year"),
        ({"first_year": True}, "first_year"),
        ({"weekdays": "MON"}, "weekdays"),
        ({"weekdays": []}, "weekdays"),
        ({"weekdays": ["Monday"]}, "weekdays"),
        ({"weekdays": ["MON", "MON"]}, "weekdays"),
    ],
)
def test_bad_rule_refinements_are_refused(
    instrument_path: Path, tmp_path: Path, extra: dict[str, Any], message: str
) -> None:
    rules = _yaml(instrument_path)["holiday_rules"]
    edited = [{**r, **extra} if r["name"] == "Juneteenth" else r for r in rules]
    with pytest.raises(ConfigError, match=message):
        load_instrument(rewrite(instrument_path, tmp_path, holiday_rules=edited))


def test_a_rule_refinement_loads(instrument_path: Path, tmp_path: Path) -> None:
    rules = _yaml(instrument_path)["holiday_rules"]
    profile = load_instrument(rewrite(instrument_path, tmp_path, holiday_rules=rules))
    eve = next(r for r in profile.holiday_rules if r.name == "Independence Day eve")
    assert eve.weekdays == frozenset({0, 1, 2, 3})
    assert eve.first_year is None
    plain = next(r for r in profile.holiday_rules if r.name == "Labor Day")
    assert (plain.first_year, plain.weekdays) == (None, None)


def test_the_july_3_rule_is_required(instrument_path: Path, tmp_path: Path) -> None:
    rules = [
        r for r in _yaml(instrument_path)["holiday_rules"] if r["name"] != "Independence Day eve"
    ]
    with pytest.raises(ConfigError, match="no rule for Independence Day eve"):
        load_instrument(rewrite(instrument_path, tmp_path, holiday_rules=rules))


# Review round 1, L3 (RV15): EVERY listed early close 2010-06-01..2024-12-31, with its time.
# Stock half days (day after Thanksgiving, Christmas Eve, July 3): 10:15 PT in the data;
# US-holiday sessions: 08:30 PT 2010-2012, 10:00 PT from 2014-05-26; after the data
# (2024-09-28 on): 10:00 PT. The 13:15 maintenance-halt days (a period) are not listed here.
HISTORY_EARLY_CLOSES = {
    "2010-07-05": "08:30",
    "2010-09-06": "08:30",
    "2010-11-25": "08:30",
    "2010-11-26": "10:15",
    "2011-01-17": "08:30",
    "2011-02-21": "08:30",
    "2011-05-30": "08:30",
    "2011-07-04": "08:30",
    "2011-09-05": "08:30",
    "2011-11-24": "08:30",
    "2011-11-25": "10:15",
    "2012-01-16": "08:30",
    "2012-02-20": "08:30",
    "2012-05-28": "08:30",
    "2012-07-03": "10:15",
    "2012-07-04": "08:30",
    "2012-09-03": "08:30",
    "2012-11-23": "10:15",
    "2012-12-24": "10:15",
    "2013-07-03": "10:15",
    "2013-11-29": "10:15",
    "2013-12-24": "10:15",
    "2014-05-26": "10:00",
    "2014-07-03": "10:15",
    "2014-07-04": "10:00",
    "2014-09-01": "10:00",
    "2014-11-27": "10:00",
    "2014-11-28": "10:15",
    "2014-12-24": "10:15",
    "2015-01-19": "10:00",
    "2015-02-16": "10:00",
    "2015-05-25": "10:00",
    "2015-07-03": "10:00",
    "2015-09-07": "10:00",
    "2015-11-26": "10:00",
    "2015-11-27": "10:15",
    "2015-12-24": "10:15",
    "2016-01-18": "10:00",
    "2016-02-15": "10:00",
    "2016-05-30": "10:00",
    "2016-07-04": "10:00",
    "2016-09-05": "10:00",
    "2016-11-24": "10:00",
    "2016-11-25": "10:15",
    "2017-01-16": "10:00",
    "2017-02-20": "10:00",
    "2017-05-29": "10:00",
    "2017-07-03": "10:15",
    "2017-07-04": "10:00",
    "2017-09-04": "10:00",
    "2017-11-23": "10:00",
    "2017-11-24": "10:15",
    "2018-01-15": "10:00",
    "2018-02-19": "10:00",
    "2018-05-28": "10:00",
    "2018-07-03": "10:15",
    "2018-07-04": "10:00",
    "2018-09-03": "10:00",
    "2018-11-22": "10:00",
    "2018-11-23": "10:15",
    "2018-12-24": "10:15",
    "2019-01-21": "10:00",
    "2019-02-18": "10:00",
    "2019-05-27": "10:00",
    "2019-07-03": "10:15",
    "2019-07-04": "10:00",
    "2019-09-02": "10:00",
    "2019-11-28": "10:00",
    "2019-11-29": "10:15",
    "2019-12-24": "10:15",
    "2020-01-20": "10:00",
    "2020-02-17": "10:00",
    "2020-05-25": "10:00",
    "2020-07-03": "10:00",
    "2020-09-07": "10:00",
    "2020-11-26": "10:00",
    "2020-11-27": "10:15",
    "2020-12-24": "10:15",
    "2021-01-18": "10:00",
    "2021-02-15": "10:00",
    "2021-05-31": "10:00",
    "2021-07-05": "10:00",
    "2021-09-06": "10:00",
    "2021-11-25": "10:00",
    "2021-11-26": "10:15",
    "2022-01-17": "10:00",
    "2022-02-21": "10:00",
    "2022-05-30": "10:00",
    "2022-06-20": "10:00",
    "2022-07-04": "10:00",
    "2022-09-05": "10:00",
    "2022-11-24": "10:00",
    "2022-11-25": "10:15",
    "2023-01-16": "10:00",
    "2023-02-20": "10:00",
    "2023-05-29": "10:00",
    "2023-06-19": "10:00",
    "2023-07-03": "10:15",
    "2023-07-04": "10:00",
    "2023-09-04": "10:00",
    "2023-11-23": "10:00",
    "2023-11-24": "10:15",
    "2024-01-15": "10:00",
    "2024-02-19": "10:00",
    "2024-05-27": "10:00",
    "2024-06-19": "10:00",
    "2024-07-03": "10:15",
    "2024-07-04": "10:00",
    "2024-09-02": "10:00",
    "2024-11-28": "10:00",
    "2024-11-29": "10:00",
    "2024-12-24": "10:00",
}


def test_every_history_early_close_time_is_pinned(instrument_path: Path) -> None:
    listed = {
        d.isoformat(): t
        for d, t in _yaml(instrument_path)["early_closes"].items()
        if d.year <= 2024
    }
    assert listed == HISTORY_EARLY_CLOSES


def test_every_history_half_day_is_an_option_early_close_and_vice_versa(
    instrument: InstrumentProfile,
) -> None:
    half_days = {d for d, t in HISTORY_EARLY_CLOSES.items() if t == "10:15"}
    option = {d.isoformat() for d in instrument.option_expiry.early_close_dates}
    assert half_days == {d for d in option if d <= "2024-09-27"}  # up to the data's end
