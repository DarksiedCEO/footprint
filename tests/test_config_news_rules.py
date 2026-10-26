"""config/news.yaml and config/fomc.yaml (Andre's N1-N6 and the N-OPEN defaults, 2026-10-14):
loading and refusals."""

from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import yaml

from footprint.core.config import ConfigError, load_fomc, load_news_rules, load_profiles
from tests.conftest import REPO_ROOT

NEWS = REPO_ROOT / "config" / "news.yaml"
FOMC = REPO_ROOT / "config" / "fomc.yaml"
FED_PAGE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# Read on 2026-10-26 from the Federal Reserve's pages: the second day of every scheduled
# meeting held, 2017-2020 from fomchistoricalYYYY.htm, 2021-2027 from FED_PAGE. 2027 is
# "tentative until confirmed". 2020's March 17-18 meeting was cancelled (CANCELLED).
HISTORY = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{}.htm"
FED_DECISION_DAYS = {
    2017: ["02-01", "03-15", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2018: ["01-31", "03-21", "05-02", "06-13", "08-01", "09-26", "11-08", "12-19"],
    2019: ["01-30", "03-20", "05-01", "06-19", "07-31", "09-18", "10-30", "12-11"],
    2020: ["01-29", "04-29", "06-10", "07-29", "09-16", "11-05", "12-16"],
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
    2027: ["01-27", "03-17", "04-28", "06-09", "07-28", "09-15", "10-27", "12-08"],
}


def _write(tmp_path: Path, profile: Path, **changes: Any) -> Path:
    data = yaml.safe_load(profile.read_text())
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    target = tmp_path / profile.name
    target.write_text(yaml.safe_dump(data))
    return target


RELEASE = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{}a.htm"
# Intermeeting actions on the Fed's pages (unscheduled meetings, notation votes) and
# the announcement days that differ, each confirmed on federalreserve.gov.
INTERMEETING = {
    date(2019, 10, 4): HISTORY.format(2019),
    date(2019, 10, 11): RELEASE.format("20191011"),
    date(2020, 3, 2): HISTORY.format(2020),
    date(2020, 3, 3): RELEASE.format("20200303"),
    date(2020, 3, 15): RELEASE.format("20200315"),
    date(2020, 3, 19): RELEASE.format("20200319").replace("a.htm", "b.htm"),
    date(2020, 3, 23): RELEASE.format("20200323"),
    date(2020, 3, 31): RELEASE.format("20200331"),
    date(2020, 8, 27): RELEASE.format("20200827"),
    date(2025, 8, 22): FED_PAGE,
}


def _meetings() -> list[dict[str, Any]]:
    return list(yaml.safe_load(FOMC.read_text())["meetings"])


def _meeting(first: date, decision: date, verified: object = True) -> dict[str, Any]:
    return {"first_day": first, "decision_day": decision, "verified": verified, "source": FED_PAGE}


# --- the shipped files ------------------------------------------------------------


def test_the_shipped_news_rules_load() -> None:
    rules = load_news_rules(NEWS, FOMC)
    assert rules.timezone == "America/New_York"
    assert (rules.lead_minutes, rules.lag_minutes) == (5, 2)
    assert [(e.name, e.at) for e in rules.daily_events] == [
        ("8:30 ET releases (N1)", time(8, 30)),
        ("cash open (N3)", time(9, 30)),
        ("10:00 ET releases (N1)", time(10, 0)),
    ]
    assert rules.pre_news_until == "10:00 ET releases (N1)"
    assert rules.time_stop_after_daily is True
    assert rules.overnight_flat_from == time(15, 0)
    assert rules.fomc_flat_from == time(13, 55)
    assert rules.fomc_warn_days == 30


def test_the_shipped_fomc_dates_are_the_feds_own() -> None:
    fomc = load_fomc(FOMC)
    assert fomc.source == FED_PAGE
    assert (fomc.valid_from, fomc.valid_through) == (date(2017, 1, 1), date(2027, 12, 31))
    expected = [
        date.fromisoformat(f"{year}-{md}")
        for year, days in FED_DECISION_DAYS.items()
        for md in days
    ]
    assert [m.decision_day for m in fomc.meetings] == expected
    for m in fomc.meetings:
        assert (m.decision_day - m.first_day).days == 1  # two-day meetings
        # tentative on the Fed's page (after the Oct 27-28 2026 meeting, still to be
        # held when read): UNVERIFIED (a whole-day blackout)
        assert m.verified is (m.decision_day < date(2026, 12, 9))
        year = m.decision_day.year
        if year < 2021:
            assert m.source == HISTORY.format(year)
        elif year < 2027:
            assert m.source == FED_PAGE
        else:
            assert m.source == RELEASE.format("20250905")
    assert [(c.first_day, c.decision_day, c.source) for c in fomc.cancelled] == [
        (date(2020, 3, 17), date(2020, 3, 18), HISTORY.format(2020))
    ]
    assert {a.day: a.source for a in fomc.intermeeting} == INTERMEETING
    assert all(a.verified for a in fomc.intermeeting)


def test_profiles_carry_the_news_rules() -> None:
    p = load_profiles(REPO_ROOT / "config", "nq.yaml")
    assert p.news_rules == load_news_rules(NEWS, FOMC)


# --- news.yaml refusals -----------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"timezone": "Mars/Base"}, "unknown timezone"),
        ({"lead_minutes": -1}, "lead_minutes must be >= 0"),
        ({"lag_minutes": -1}, "lag_minutes must be >= 0"),
        ({"lead_minutes": True}, "lead_minutes must be an integer"),
        ({"daily_events": []}, "daily_events must be a non-empty list"),
        ({"daily_events": "x"}, "daily_events must be a non-empty list"),
        ({"daily_events": [{"name": "a", "at": "08:30"}, {"name": "a", "at": "09:00"}]}, "twice"),
        ({"daily_events": [{"name": "a"}]}, "missing keys"),
        ({"daily_events": [{"name": "a", "at": "8h30"}]}, "must be HH:MM"),
        ({"daily_events": [{"name": " ", "at": "08:30"}]}, "non-empty string"),
        ({"pre_news_until": "no such event"}, "pre_news_until"),
        ({"time_stop_after_daily": "yes"}, "true or false"),
        ({"overnight_flat_from": "15:00:30"}, "no seconds"),
        ({"fomc_flat_from": "13:55:30"}, "no seconds"),
        ({"fomc_warn_days": -1}, "fomc_warn_days must be >= 0"),
        ({"overnight_flat_from": 1500}, "non-empty string"),
        ({"surprise": 1}, "unknown keys"),
        ({"lag_minutes": ...}, "missing keys"),
    ],
)
def test_bad_news_rules_are_refused(tmp_path: Path, changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_news_rules(_write(tmp_path, NEWS, **changes), FOMC)


# --- fomc.yaml refusals (N6: the Fed's calendar only, fail closed) -------------------


def test_an_fomc_source_that_is_not_the_feds_own_site_is_refused(tmp_path: Path) -> None:
    for source in (
        "https://fedratecalc.com/fomc-meeting-schedule/",
        "http://www.federalreserve.gov/x",
    ):
        with pytest.raises(ConfigError, match="Federal Reserve"):
            load_fomc(_write(tmp_path, FOMC, source=source))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"valid_from": date(2028, 1, 1)}, "valid_from must not be after valid_through"),
        ({"valid_through": "2027-12-31"}, "must be a date"),
        ({"meetings_per_year": 0}, "meetings_per_year must be >= 1"),
        ({"meetings": []}, "meetings must be a non-empty list"),
        ({"read_on": ...}, "missing keys"),
    ],
)
def test_bad_fomc_calendars_are_refused(
    tmp_path: Path, changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_fomc(_write(tmp_path, FOMC, **changes))


def test_a_missing_meeting_in_a_full_year_is_refused(tmp_path: Path) -> None:
    meetings = [m for m in _meetings() if m["decision_day"] != date(2024, 9, 18)]
    with pytest.raises(ConfigError, match="2024 lists 7 meetings, not 8"):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


def test_an_extra_meeting_in_a_full_year_is_refused(tmp_path: Path) -> None:
    extra = _meeting(date(2025, 8, 21), date(2025, 8, 22))
    meetings = sorted([*_meetings(), extra], key=lambda m: m["decision_day"])
    with pytest.raises(ConfigError, match="2025 lists 9 meetings, not 8"):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


def test_a_span_ending_inside_a_year_is_checked_by_its_gaps(tmp_path: Path) -> None:
    """valid_through inside a year: the year cannot be counted, but every gap can."""
    meetings = [m for m in _meetings() if m["decision_day"] <= date(2027, 6, 30)]
    fomc = load_fomc(_write(tmp_path, FOMC, valid_through=date(2027, 6, 30), meetings=meetings))
    assert fomc.meetings[-1].decision_day == date(2027, 6, 9)


def test_m1_a_span_extended_past_its_meetings_is_refused(tmp_path: Path) -> None:
    """Review M1: extending valid_through without listing the new year's meetings
    (the edit the expiry alarm asks for) must fail, not leave FOMC days unprotected."""
    with pytest.raises(ConfigError, match="max_gap_days"):
        load_fomc(_write(tmp_path, FOMC, valid_through=date(2028, 6, 30)))


def test_m1_a_span_started_before_its_meetings_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_gap_days"):
        load_fomc(_write(tmp_path, FOMC, valid_from=date(2016, 7, 1)))


def test_m1_a_missing_meeting_inside_the_span_is_refused_by_its_gap(tmp_path: Path) -> None:
    """Even where the year count cannot see it (valid_through mid-year)."""
    meetings = [
        m
        for m in _meetings()
        if m["decision_day"] <= date(2027, 6, 30) and m["decision_day"] != date(2027, 4, 28)
    ]
    with pytest.raises(ConfigError, match="max_gap_days"):
        load_fomc(_write(tmp_path, FOMC, valid_through=date(2027, 6, 30), meetings=meetings))


def test_m1_the_gap_limit_is_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_gap_days 55"):
        load_fomc(_write(tmp_path, FOMC, max_gap_days=55))  # the listed maximum is 56
    assert load_fomc(_write(tmp_path, FOMC, max_gap_days=56)).max_gap_days == 56
    with pytest.raises(ConfigError, match="max_gap_days must be >= 1"):
        load_fomc(_write(tmp_path, FOMC, max_gap_days=0))


def test_partial_first_year_is_not_counted(tmp_path: Path) -> None:
    """valid_from after 1 January: that year cannot be checked (seven meetings left)."""
    meetings = [m for m in _meetings() if m["decision_day"] >= date(2017, 2, 2)]
    fomc = load_fomc(_write(tmp_path, FOMC, valid_from=date(2017, 2, 2), meetings=meetings))
    assert fomc.meetings[0].decision_day == date(2017, 3, 15)


@pytest.mark.parametrize(
    ("index", "meeting", "match"),
    [
        (0, _meeting(date(2017, 2, 2), date(2017, 2, 1)), "the day after its first_day"),
        (0, _meeting(date(2016, 12, 13), date(2016, 12, 14)), "outside valid_from"),
        (-1, _meeting(date(2028, 1, 25), date(2028, 1, 26), False), "outside valid_from"),
        (1, _meeting(date(2017, 1, 31), date(2017, 2, 1)), "in date order"),
        (0, _meeting(date(2017, 1, 31), date(2017, 2, 1), "yes"), "true or false"),
        (0, {"first_day": date(2017, 1, 31), "decision_day": date(2017, 2, 1)}, "missing keys"),
        (
            0,
            {**_meeting(date(2017, 1, 31), date(2017, 2, 1)), "source": "https://x.org/"},
            "Federal Reserve",
        ),
        (0, "2017-02-01", "expected a mapping"),
    ],
)
def test_bad_meetings_are_refused(tmp_path: Path, index: int, meeting: object, match: str) -> None:
    meetings: list[object] = list(_meetings())
    meetings[index] = meeting
    with pytest.raises(ConfigError, match=match):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


# --- cancelled meetings and intermeeting actions ------------------------------------


def _calendar() -> dict[str, Any]:
    return dict(yaml.safe_load(FOMC.read_text()))


def _write_calendar(tmp_path: Path, data: dict[str, Any]) -> Path:
    target = tmp_path / "fomc.yaml"
    target.write_text(yaml.safe_dump(data))
    return target


def test_a_cancelled_meeting_counts_toward_its_year(tmp_path: Path) -> None:
    data = _calendar()
    data["cancelled"] = []
    with pytest.raises(ConfigError, match="2020 lists 7 meetings, not 8"):
        load_fomc(_write_calendar(tmp_path, data))


def test_a_cancelled_meeting_on_a_held_meetings_day_is_refused(tmp_path: Path) -> None:
    data = _calendar()
    data["cancelled"] = [
        {**data["cancelled"][0], "first_day": date(2020, 4, 28), "decision_day": date(2020, 4, 29)}
    ]
    with pytest.raises(ConfigError, match="in date order"):
        load_fomc(_write_calendar(tmp_path, data))


@pytest.mark.parametrize(
    ("action", "match"),
    [
        ({"day": date(2016, 12, 30), "verified": True, "source": FED_PAGE}, "outside valid_from"),
        ({"day": date(2020, 3, 3), "verified": True, "source": FED_PAGE}, "in date order"),
        ({"day": date(2020, 3, 4), "verified": 1, "source": FED_PAGE}, "true or false"),
        (
            {"day": date(2020, 3, 4), "verified": True, "source": "https://x.org/"},
            "Federal Reserve",
        ),
        ({"day": date(2020, 3, 4), "verified": True}, "missing keys"),
    ],
)
def test_bad_intermeeting_actions_are_refused(
    tmp_path: Path, action: dict[str, Any], match: str
) -> None:
    data = _calendar()
    actions = list(data["intermeeting"])
    data["intermeeting"] = sorted([*actions, action], key=lambda a: a["day"])
    with pytest.raises(ConfigError, match=match):
        load_fomc(_write_calendar(tmp_path, data))


def test_out_of_order_intermeeting_actions_are_refused(tmp_path: Path) -> None:
    data = _calendar()
    data["intermeeting"] = list(reversed(data["intermeeting"]))
    with pytest.raises(ConfigError, match="in date order"):
        load_fomc(_write_calendar(tmp_path, data))


def test_no_intermeeting_actions_and_no_cancellations_may_be_empty_lists(tmp_path: Path) -> None:
    data = _calendar()
    data["intermeeting"] = []
    data["cancelled"] = []
    data["valid_from"] = date(2021, 1, 1)
    data["meetings"] = [m for m in data["meetings"] if m["decision_day"] >= date(2021, 1, 1)]
    fomc = load_fomc(_write_calendar(tmp_path, data))
    assert (fomc.cancelled, fomc.intermeeting) == ((), ())


# --- review round 1: L2 source host, L3 meeting shape, verified must be a bool --------


@pytest.mark.parametrize(
    "source",
    [
        "https://www.federalreserve.gov.evil.com/monetarypolicy/fomccalendars.htm",
        "https://www.federalreserve.gov@evil.com/monetarypolicy/fomccalendars.htm",
        "https://www.federalreserve.gov:8443/monetarypolicy/fomccalendars.htm",
        "http://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "https://federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "https://WWW.FEDERALRESERVE.GOV/monetarypolicy/fomccalendars.htm",
        "www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    ],
)
def test_l2_only_https_on_the_feds_own_host_is_accepted(tmp_path: Path, source: str) -> None:
    with pytest.raises(ConfigError, match="Federal Reserve"):
        load_fomc(_write(tmp_path, FOMC, source=source))
    meetings = _meetings()
    meetings[0] = {**meetings[0], "source": source}
    with pytest.raises(ConfigError, match="Federal Reserve"):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


@pytest.mark.parametrize(
    ("first", "decision", "match"),
    [
        (date(2017, 2, 1), date(2017, 2, 1), "the day after its first_day"),  # one-day
        (date(2017, 1, 22), date(2017, 2, 1), "the day after its first_day"),  # too long
        (date(2017, 2, 3), date(2017, 2, 4), "weekday"),  # a Saturday
        (date(2017, 2, 4), date(2017, 2, 5), "weekday"),  # a Sunday
    ],
)
def test_l3_a_meeting_must_be_two_days_ending_on_a_weekday(
    tmp_path: Path, first: date, decision: date, match: str
) -> None:
    meetings = _meetings()
    meetings[0] = {**meetings[0], "first_day": first, "decision_day": decision}
    with pytest.raises(ConfigError, match=match):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


def test_l3_a_cancelled_meeting_has_the_same_shape(tmp_path: Path) -> None:
    data = _calendar()
    data["cancelled"] = [{**data["cancelled"][0], "first_day": date(2020, 3, 16)}]
    with pytest.raises(ConfigError, match="the day after its first_day"):
        load_fomc(_write_calendar(tmp_path, data))


def test_l3_a_meeting_overlapping_the_previous_one_is_refused(tmp_path: Path) -> None:
    """2017-03-14/15 then a meeting starting on 2017-03-15 itself."""
    meetings = _meetings()
    meetings.insert(
        2,
        {**meetings[1], "first_day": date(2017, 3, 15), "decision_day": date(2017, 3, 16)},
    )
    with pytest.raises(ConfigError, match="overlaps"):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "y", "1"])
def test_verified_must_be_written_true_or_false(tmp_path: Path, word: str) -> None:
    """YAML 1.1 reads yes/on as true: the FOMC file accepts only the words true/false."""
    text = FOMC.read_text()
    assert "verified: true," in text
    target = tmp_path / "fomc.yaml"
    target.write_text(text.replace("verified: true,", f"verified: {word},", 1))
    with pytest.raises(ConfigError, match="true or false"):
        load_fomc(target)
