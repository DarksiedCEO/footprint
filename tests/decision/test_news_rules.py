"""The scheduled news and opening-bell windows (Andre's N1-N4, N7, 2026-10-14) and the
N-OPEN-1..3 defaults. Times in these tests are written out by hand from the rules, not
computed from config, so a config or code change that moves a window fails here."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from footprint.core.config import (
    ConfigError,
    DailyEvent,
    NewsRulesSettings,
    Profiles,
    Session,
    load_profiles,
)
from footprint.decision.news_rules import NewsRules, with_news_rules
from footprint.decision.session import NewsBlackout, in_news_blackout
from tests.conftest import REPO_ROOT

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
US = timedelta(microseconds=1)
WINTER = date(2026, 1, 6)  # a Tuesday on standard time
SUMMER = date(2026, 7, 7)  # a Tuesday on daylight time


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="module")
def rules(profiles: Profiles) -> NewsRules:
    return NewsRules(profiles.news_rules, profiles.instrument)


def at(day: date, hh: int, mm: int, zone: ZoneInfo = ET, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), zone).astimezone(UTC)


def end_of_minute(day: date, hh: int, mm: int, zone: ZoneInfo = ET) -> datetime:
    return at(day, hh, mm, zone) + timedelta(minutes=1) - US


def named(rules: NewsRules, day: date, name: str) -> NewsBlackout:
    found = [w for w in rules.day(day) if w.name == name]
    assert len(found) == 1, [w.name for w in rules.day(day)]
    return found[0]


# --- N1, N2: the fixed daily release windows ------------------------------------------


@pytest.mark.parametrize("day", [WINTER, SUMMER])
def test_n1_eight_thirty_window_is_0825_to_0832_et_with_the_whole_end_minute(
    rules: NewsRules, day: date
) -> None:
    w = named(rules, day, "8:30 ET releases (N1)")
    assert w.start == at(day, 8, 25)
    assert w.end == end_of_minute(day, 8, 32)
    assert w.covers(at(day, 8, 32, ss=59))
    assert not w.covers(at(day, 8, 33))
    assert not w.covers(at(day, 8, 25) - US)


@pytest.mark.parametrize("day", [WINTER, SUMMER])
def test_n1_ten_oclock_window_is_0955_to_1002_et(rules: NewsRules, day: date) -> None:
    w = named(rules, day, "10:00 ET releases (N1)")
    assert (w.start, w.end) == (at(day, 9, 55), end_of_minute(day, 10, 2))


def test_n1_in_pacific_time_is_0525_to_0532(rules: NewsRules) -> None:
    w = named(rules, SUMMER, "8:30 ET releases (N1)")
    assert (w.start, w.end) == (at(SUMMER, 5, 25, PT), end_of_minute(SUMMER, 5, 32, PT))


def test_n2_edges_come_from_config(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, lead_minutes=7, lag_minutes=4)
    w = named(NewsRules(settings, profiles.instrument), SUMMER, "8:30 ET releases (N1)")
    assert (w.start, w.end) == (at(SUMMER, 8, 23), end_of_minute(SUMMER, 8, 34))


def test_zero_edges_are_the_release_minute_alone(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, lead_minutes=0, lag_minutes=0)
    w = named(NewsRules(settings, profiles.instrument), SUMMER, "8:30 ET releases (N1)")
    assert (w.start, w.end) == (at(SUMMER, 8, 30), end_of_minute(SUMMER, 8, 30))


# --- N3: the opening bell ------------------------------------------------------------


def test_n3_cash_open_window_is_0625_to_0632_pt(rules: NewsRules) -> None:
    w = named(rules, SUMMER, "cash open (N3)")
    assert (w.start, w.end) == (at(SUMMER, 6, 25, PT), end_of_minute(SUMMER, 6, 32, PT))


# --- N4 with N-OPEN-1: no entries from the prime start to the 10:02 ET window's end ---


def test_n4_every_day_from_the_prime_start_to_the_end_of_the_ten_oclock_window(
    rules: NewsRules,
) -> None:
    for day in (WINTER, SUMMER, date(2026, 7, 11)):  # Saturday too (N7)
        w = named(rules, day, "no entries before news (N4) until 10:00 ET releases (N1)")
        assert (w.start, w.end) == (at(day, 4, 0, PT), end_of_minute(day, 10, 2))


def test_n4_until_another_event_follows_config(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, pre_news_until="8:30 ET releases (N1)")
    rules = NewsRules(settings, profiles.instrument)
    w = named(rules, SUMMER, "no entries before news (N4) until 8:30 ET releases (N1)")
    assert w.end == end_of_minute(SUMMER, 8, 32)


def test_n4_ending_before_the_prime_start_is_refused(profiles: Profiles) -> None:
    events = (*profiles.news_rules.daily_events,)
    early = replace(events[0], name="early", at=time(6, 0))
    settings = replace(profiles.news_rules, daily_events=(early, *events), pre_news_until="early")
    with pytest.raises(ConfigError, match="N4"):
        NewsRules(settings, profiles.instrument)


# --- N7: no rule depends on the weekday -----------------------------------------------


def test_n7_every_day_of_the_week_has_the_same_daily_windows(rules: NewsRules) -> None:
    week = [SUMMER + timedelta(days=k) for k in range(7)]

    def clock(day: date) -> list[tuple[str, time, time]]:
        return [
            (w.name, w.start.astimezone(ET).time(), w.end.astimezone(ET).time())
            for w in rules.day(day)
        ]

    assert all(clock(day) == clock(SUMMER) for day in week)
    assert len(clock(SUMMER)) == 5  # three daily events, N4, the overnight window


# --- N-OPEN-3: the overnight flat window -----------------------------------------------


def test_n_open_3_overnight_flat_from_the_overnight_start_to_its_end(rules: NewsRules) -> None:
    w = named(rules, SUMMER, "overnight flat window (N-OPEN-3)")
    nxt = SUMMER + timedelta(days=1)
    assert (w.start, w.end) == (at(SUMMER, 15, 0, PT), end_of_minute(nxt, 3, 59, PT))


def test_n_open_3_a_start_after_midnight_ends_the_same_day(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, overnight_flat_from=time(3, 0))
    w = named(NewsRules(settings, profiles.instrument), SUMMER, "overnight flat window (N-OPEN-3)")
    assert (w.start, w.end) == (at(SUMMER, 3, 0, PT), end_of_minute(SUMMER, 3, 59, PT))


@pytest.mark.parametrize("start", [time(4, 0), time(14, 59), time(10, 0), time(3, 59)])
def test_n_open_3_a_start_outside_the_overnight_session_is_refused(
    profiles: Profiles, start: time
) -> None:
    settings = replace(profiles.news_rules, overnight_flat_from=start)
    with pytest.raises(ConfigError, match="overnight_flat_from"):
        NewsRules(settings, profiles.instrument)


# --- N-OPEN-2: which daily windows start the news time stop (per window) ------------


EIGHT_THIRTY = "8:30 ET releases (N1)"
OVERNIGHT = "overnight flat window (N-OPEN-3)"


def narrow(settings: NewsRulesSettings) -> NewsRulesSettings:
    """N-OPEN-2 setting (b): the time stop only after the 8:30 ET window, every day."""
    events = tuple(replace(e, time_stop=e.name == EIGHT_THIRTY) for e in settings.daily_events)
    return replace(settings, daily_events=events, pre_news_time_stop=False)


def test_n_open_2_built_default_every_daily_window_carries_the_time_stop(
    rules: NewsRules,
) -> None:
    assert {w.name: w.time_stop for w in rules.day(SUMMER)} == {
        EIGHT_THIRTY: True,
        "cash open (N3)": True,
        "10:00 ET releases (N1)": True,
        "no entries before news (N4) until 10:00 ET releases (N1)": True,
        OVERNIGHT: True,
    }


def test_n_open_2_narrow_reading_only_the_eight_thirty_window_carries_it(
    profiles: Profiles,
) -> None:
    rules = NewsRules(narrow(profiles.news_rules), profiles.instrument)
    assert {w.name: w.time_stop for w in rules.day(SUMMER)} == {
        EIGHT_THIRTY: True,
        "cash open (N3)": False,
        "10:00 ET releases (N1)": False,
        "no entries before news (N4) until 10:00 ET releases (N1)": False,
        OVERNIGHT: True,  # not a daily window: always carries it
    }


def test_n_open_2_setting_c_release_days_only_no_daily_window_carries_it(
    profiles: Profiles,
) -> None:
    """Setting (c): every switch false; only the overnight window (and any CSV window)
    carries the time stop."""
    s = profiles.news_rules
    events = tuple(replace(e, time_stop=False) for e in s.daily_events)
    rules = NewsRules(
        replace(s, daily_events=events, pre_news_time_stop=False), profiles.instrument
    )
    assert {w.name for w in rules.day(SUMMER) if w.time_stop} == {OVERNIGHT}


def test_n_open_2_each_daily_window_follows_its_own_switch(profiles: Profiles) -> None:
    for k, event in enumerate(profiles.news_rules.daily_events):
        events = tuple(
            replace(e, time_stop=j == k) for j, e in enumerate(profiles.news_rules.daily_events)
        )
        settings = replace(profiles.news_rules, daily_events=events, pre_news_time_stop=k == 0)
        found = {w.name: w.time_stop for w in NewsRules(settings, profiles.instrument).day(SUMMER)}
        assert [found[e.name] for e in events] == [j == k for j in range(len(events))], event
        assert found["no entries before news (N4) until 10:00 ET releases (N1)"] is (k == 0)
        assert found[OVERNIGHT] is True


# --- windows at a moment, and the combination with a CSV -------------------------------


def test_windows_at_a_moment_cover_yesterdays_overnight_and_todays_rules(
    rules: NewsRules,
) -> None:
    two_am = at(SUMMER, 2, 0, PT)  # inside the overnight window that began yesterday
    hit = in_news_blackout(two_am, rules.windows(two_am))
    assert hit is not None
    assert hit.name == "overnight flat window (N-OPEN-3)"
    news = at(SUMMER, 8, 30)
    hit = in_news_blackout(news, rules.windows(news))
    assert hit is not None
    assert hit.name == "8:30 ET releases (N1)"
    assert in_news_blackout(at(SUMMER, 11, 0), rules.windows(at(SUMMER, 11, 0))) is None
    late = at(SUMMER, 23, 30)  # ET evening: tomorrow's rules are listed too
    names = {w.name for w in rules.windows(late)}
    assert sum(1 for w in rules.windows(late) if w.name == "8:30 ET releases (N1)") == 3
    assert "overnight flat window (N-OPEN-3)" in names


def test_windows_refuse_a_naive_moment(rules: NewsRules) -> None:
    with pytest.raises(ValueError, match="UTC"):
        rules.windows(datetime(2026, 7, 7, 12, 0))  # noqa: DTZ001


def test_windows_are_built_once_per_day(rules: NewsRules) -> None:
    assert rules.day(SUMMER) is rules.day(SUMMER)


def test_with_news_rules_adds_the_csv_windows(profiles: Profiles) -> None:
    csv = NewsBlackout("CPI", at(SUMMER, 11, 0), at(SUMMER, 11, 5))
    combined = with_news_rules(profiles, lambda now: (csv,))
    now = at(SUMMER, 11, 1)
    found = combined(now)
    assert csv in found
    assert set(NewsRules(profiles.news_rules, profiles.instrument).windows(now)) <= set(found)


def test_with_news_rules_lets_a_csv_failure_through(profiles: Profiles) -> None:
    """The runner turns any failure into a blackout in force (R3-1)."""

    def broken(now: datetime) -> tuple[NewsBlackout, ...]:
        raise OSError("gone")

    with pytest.raises(OSError, match="gone"):
        with_news_rules(profiles, broken)(at(SUMMER, 11, 0))


def test_a_blackout_time_stop_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="time_stop must be a bool"):
        NewsBlackout("x", at(SUMMER, 8, 0), at(SUMMER, 8, 1), time_stop=1)  # type: ignore[arg-type]
    assert NewsBlackout("x", at(SUMMER, 8, 0), at(SUMMER, 8, 1)).time_stop is True


# --- review round 1 M2: survivors R1, R2, R3, R5 ---------------------------------


def _tokyo(profiles: Profiles) -> NewsRules:
    """A news clock east of UTC (its date runs ahead of UTC's), with one event late in
    the Tokyo evening so N4 (from 04:00 PT) is never empty."""
    late = DailyEvent("late", time(23, 30), True)
    settings = replace(
        profiles.news_rules,
        timezone="Asia/Tokyo",
        daily_events=(late,),
        pre_news_until="late",
    )
    return NewsRules(settings, profiles.instrument)


@pytest.mark.parametrize(
    ("which", "moment", "news_day"),
    [
        # 23:30 ET on 7 Jul is 03:30 UTC on 8 Jul: the news day is the 7th
        ("shipped", datetime(2026, 7, 8, 3, 30, tzinfo=UTC), date(2026, 7, 7)),
        # 08:00 JST on 8 Jul is 23:00 UTC on 7 Jul: the news day is the 8th
        ("tokyo", datetime(2026, 7, 7, 23, 0, tzinfo=UTC), date(2026, 7, 8)),
    ],
)
def test_windows_are_the_news_clock_days_around_the_moment(
    profiles: Profiles, rules: NewsRules, which: str, moment: datetime, news_day: date
) -> None:
    """R1: the day before, of and after `now` on the NEWS clock, not on UTC's."""
    r = rules if which == "shipped" else _tokyo(profiles)
    one = timedelta(days=1)
    expected = (*r.day(news_day - one), *r.day(news_day), *r.day(news_day + one))
    assert r.windows(moment) == expected


def test_an_overnight_session_not_crossing_midnight(profiles: Profiles) -> None:
    """R2: an overnight session inside one calendar day: a flat window may start at its
    first minute, and ends that same day."""
    night = Session(time(0, 0), time(3, 59), "America/Los_Angeles")
    inst = replace(profiles.instrument, overnight_session=night)
    settings = replace(profiles.news_rules, overnight_flat_from=time(0, 0))
    w = named(NewsRules(settings, inst), SUMMER, OVERNIGHT)
    assert (w.start, w.end) == (at(SUMMER, 0, 0, PT), end_of_minute(SUMMER, 3, 59, PT))
    for outside in (time(23, 0), time(4, 0)):
        with pytest.raises(ConfigError, match="overnight_flat_from"):
            NewsRules(replace(settings, overnight_flat_from=outside), inst)


def test_the_scheduled_windows_come_before_the_csv_ones(profiles: Profiles) -> None:
    """R3: where a CSV window overlaps a scheduled one, the scheduled one is listed
    first, so it is the one the precheck and the NEWS exit name (pinned order)."""
    csv = NewsBlackout("CPI", at(SUMMER, 8, 29), at(SUMMER, 8, 40))
    now = at(SUMMER, 8, 30)
    found = with_news_rules(profiles, lambda t: (csv,))(now)
    scheduled = NewsRules(profiles.news_rules, profiles.instrument).windows(now)
    assert found == (*scheduled, csv)
    hit = in_news_blackout(now, found)
    assert hit is not None
    assert hit.name == EIGHT_THIRTY


def _london(profiles: Profiles, through: date) -> None:
    """A news clock whose summer time starts on another day than the US's: 11:30 London
    is after 04:00 PT only between the US change (8 Mar 2026) and the UK one (29 Mar)."""
    event = DailyEvent("london", time(11, 30), True)
    settings = replace(
        profiles.news_rules,
        timezone="Europe/London",
        lead_minutes=0,
        lag_minutes=0,
        daily_events=(event,),
        pre_news_until="london",
    )
    inst = replace(profiles.instrument, calendar_from=date(2026, 3, 8), calendar_through=through)
    NewsRules(settings, inst)


def test_the_last_calendar_day_is_checked_at_start_up(profiles: Profiles) -> None:
    """R5: N4 is empty only on 29 Mar 2026 (the calendar's last day here): refused."""
    _london(profiles, date(2026, 3, 28))  # every day sound
    with pytest.raises(ConfigError, match="2026-03-29"):
        _london(profiles, date(2026, 3, 29))
