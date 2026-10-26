"""The scheduled news, opening-bell and Fed windows (Andre's N1-N7, 2026-10-14) and the
N-OPEN-1..3 defaults. Times in these tests are written out by hand from the rules, not
computed from config, so a config or code change that moves a window fails here."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from footprint.core.config import ConfigError, FomcMeeting, Profiles, load_profiles
from footprint.decision.news_rules import NewsRules, fomc_warning, with_news_rules
from footprint.decision.session import NewsBlackout, in_news_blackout
from footprint.execution.channels import is_critical
from tests.conftest import REPO_ROOT

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
US = timedelta(microseconds=1)
WINTER = date(2026, 1, 6)  # a Tuesday on standard time
SUMMER = date(2026, 7, 7)  # a Tuesday on daylight time
FOMC_DAY = date(2026, 9, 16)  # decision day (second day) of the 15-16 September meeting


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


# --- N5: FOMC decision day -------------------------------------------------------------


def test_n5_fomc_decision_day_flat_by_1055_pt_to_the_prime_close(rules: NewsRules) -> None:
    w = named(rules, FOMC_DAY, f"FOMC decision day {FOMC_DAY} (N5)")
    assert w.start == at(FOMC_DAY, 10, 55, PT)
    assert w.end == end_of_minute(FOMC_DAY, 14, 0, PT)
    assert not w.covers(at(FOMC_DAY, 15, 0, PT))  # the overnight session trades again
    assert w.time_stop


def test_n5_not_on_the_meetings_first_day_or_an_ordinary_day(rules: NewsRules) -> None:
    for day in (FOMC_DAY - timedelta(days=1), SUMMER):
        assert not [w for w in rules.day(day) if "FOMC" in w.name]


def test_n5_ends_at_an_early_close(profiles: Profiles) -> None:
    """2021-06-16 is inside the BH-3 halt period: prime closed at 13:15 PT."""
    rules = NewsRules(profiles.news_rules, profiles.instrument)
    day = date(2021, 6, 16)
    w = named(rules, day, f"FOMC decision day {day} (N5)")
    assert (w.start, w.end) == (at(day, 10, 55, PT), end_of_minute(day, 13, 15, PT))


def test_n5_on_a_day_closing_before_the_flat_time_adds_nothing(profiles: Profiles) -> None:
    """An early close before 10:55 PT: the prime close already flattens."""
    day = date(2026, 11, 27)  # early close 10:00 PT
    assert profiles.instrument.early_closes[day] < time(10, 55)
    meetings = tuple(
        FomcMeeting(day - timedelta(days=1), day, True, m.source)
        if m.decision_day == date(2026, 12, 9)
        else m
        for m in profiles.news_rules.fomc.meetings
    )
    fomc = replace(profiles.news_rules.fomc, meetings=meetings)
    rules = NewsRules(replace(profiles.news_rules, fomc=fomc), profiles.instrument)
    assert not [w for w in rules.day(day) if "FOMC" in w.name]


def test_n5_flat_time_follows_config(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, fomc_flat_from=time(13, 30))
    w = named(
        NewsRules(settings, profiles.instrument), FOMC_DAY, f"FOMC decision day {FOMC_DAY} (N5)"
    )
    assert w.start == at(FOMC_DAY, 13, 30)


# --- N6: fail closed -------------------------------------------------------------------


def test_n6_an_unverified_date_is_a_blackout_all_day(rules: NewsRules) -> None:
    day = date(2027, 1, 27)  # tentative on the Fed's page
    w = named(rules, day, f"FOMC date {day} UNVERIFIED (N6): no new entries all day")
    assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)


def test_n6_after_valid_through_every_day_is_a_blackout(rules: NewsRules) -> None:
    for day in (date(2028, 1, 1), date(2028, 6, 6)):
        w = named(rules, day, "FOMC CALENDAR EXPIRED on 2027-12-31 (N6): no new entries")
        assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)
    assert not [w for w in rules.day(date(2027, 12, 31)) if "EXPIRED" in w.name]


def test_n6_before_valid_from_every_day_is_a_blackout(rules: NewsRules) -> None:
    day = date(2016, 12, 31)
    w = named(rules, day, "FOMC calendar starts 2017-01-01 (N6): no new entries")
    assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)
    assert not [w for w in rules.day(date(2017, 1, 1)) if "FOMC" in w.name]


def test_n6_warning_and_critical_alarm(profiles: Profiles) -> None:
    every = tuple(replace(m, verified=True) for m in profiles.news_rules.fomc.meetings)
    s = replace(profiles.news_rules, fomc=replace(profiles.news_rules.fomc, meetings=every))
    through = s.fomc.valid_through
    assert fomc_warning(through - timedelta(days=s.fomc_warn_days + 1), s) is None
    soon = fomc_warning(through - timedelta(days=s.fomc_warn_days), s)
    assert soon is not None
    assert "FOMC calendar ends 2027-12-31 (30 days)" in soon
    assert not is_critical(soon)
    expired = fomc_warning(through + timedelta(days=1), s)
    assert expired is not None
    assert expired.startswith("FOMC CALENDAR EXPIRED on 2027-12-31")
    assert is_critical(expired)


def test_n6_warns_of_an_unverified_date_ahead(profiles: Profiles) -> None:
    s = profiles.news_rules
    first = date(2027, 1, 27)
    assert fomc_warning(first - timedelta(days=s.fomc_warn_days + 1), s) is None
    ahead = fomc_warning(first - timedelta(days=s.fomc_warn_days), s)
    assert ahead is not None
    assert "FOMC date 2027-01-27 is UNVERIFIED" in ahead
    assert fomc_warning(first, s) is not None  # the day itself
    assert fomc_warning(first + timedelta(days=1), s) is None  # 2027-03-17 is 48 days on
    assert fomc_warning(date(2026, 9, 1), s) is None


def test_n5_a_2017_fomc_day_is_a_blackout_from_1355_et(rules: NewsRules) -> None:
    """BH-14: the history from 2017 has its FOMC days (fomchistorical2017.htm). In
    2017 the prime session ended at CME's 13:15 PT halt (BH-3), so the window does."""
    day = date(2017, 3, 15)
    w = named(rules, day, f"FOMC decision day {day} (N5)")
    assert (w.start, w.end) == (at(day, 13, 55), end_of_minute(day, 13, 15, PT))
    assert in_news_blackout(at(day, 13, 55), rules.windows(at(day, 13, 55))) is not None
    before = at(day, 13, 54, ss=59)
    assert in_news_blackout(before, rules.windows(before)) is None


@pytest.mark.parametrize(
    "day",
    [
        date(2020, 3, 3),  # the 10:00 ET emergency cut
        date(2020, 3, 15),  # the Sunday cut: the Sunday evening session too
        date(2020, 3, 2),  # the unscheduled meeting itself
        date(2019, 10, 11),  # notation vote announced at 11:00 ET
        date(2020, 8, 27),  # notation vote
    ],
)
def test_n6_an_intermeeting_action_day_is_a_blackout_all_day(rules: NewsRules, day: date) -> None:
    w = named(rules, day, f"FOMC intermeeting action {day} (N6): no new entries all day")
    assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)
    assert w.time_stop


def test_n6_the_day_after_an_intermeeting_action_is_not_a_blackout(rules: NewsRules) -> None:
    assert not [w for w in rules.day(date(2020, 3, 4)) if "FOMC" in w.name]


def test_n6_a_cancelled_meetings_decision_day_is_a_blackout_all_day(rules: NewsRules) -> None:
    day = date(2020, 3, 18)
    w = named(rules, day, f"FOMC meeting {day} cancelled (N6): no new entries all day")
    assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)
    assert not [x for x in rules.day(day) if "(N5)" in x.name]


def test_n6_an_unverified_intermeeting_action_is_named_so(profiles: Profiles) -> None:
    fomc = profiles.news_rules.fomc
    actions = tuple(replace(a, verified=False) for a in fomc.intermeeting)
    settings = replace(profiles.news_rules, fomc=replace(fomc, intermeeting=actions))
    day = date(2020, 3, 3)
    rules = NewsRules(settings, profiles.instrument)
    w = named(rules, day, f"FOMC intermeeting action {day} UNVERIFIED (N6): no new entries all day")
    assert (w.start, w.end) == (at(day, 0, 0), at(day + timedelta(days=1), 0, 0) - US)
    ahead = fomc_warning(date(2020, 3, 1), settings)
    assert ahead is not None
    assert "FOMC date 2020-03-02 is UNVERIFIED" in ahead


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


# --- N-OPEN-2: the news time stop after the daily windows ------------------------------


def test_n_open_2_daily_windows_carry_the_time_stop_switch(profiles: Profiles) -> None:
    for switch in (True, False):
        settings = replace(profiles.news_rules, time_stop_after_daily=switch)
        rules = NewsRules(settings, profiles.instrument)
        for w in rules.day(FOMC_DAY):
            daily = "(N1)" in w.name or "(N3)" in w.name or "(N4)" in w.name
            assert w.time_stop is (switch if daily else True), w.name


def test_other_windows_always_carry_the_time_stop(profiles: Profiles) -> None:
    settings = replace(profiles.news_rules, time_stop_after_daily=False)
    rules = NewsRules(settings, profiles.instrument)
    for day in (date(2027, 1, 27), date(2028, 1, 1), date(2016, 12, 31), date(2020, 3, 3)):
        assert all(w.time_stop for w in rules.day(day) if "FOMC" in w.name)


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


def test_an_fomc_flat_time_at_or_after_a_normal_close_is_refused(profiles: Profiles) -> None:
    """A flat time no FOMC day could reach would silently switch N5 off: refused at start."""
    for flat in (time(23, 0), time(17, 0)):  # 17:00 ET is the 14:00 PT close itself
        settings = replace(profiles.news_rules, fomc_flat_from=flat)
        with pytest.raises(ConfigError, match="fomc_flat_from"):
            NewsRules(settings, profiles.instrument)
    settings = replace(profiles.news_rules, fomc_flat_from=time(16, 59))
    w = named(
        NewsRules(settings, profiles.instrument), FOMC_DAY, f"FOMC decision day {FOMC_DAY} (N5)"
    )
    assert w.start == at(FOMC_DAY, 13, 59, PT)
