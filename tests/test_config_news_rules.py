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

# Read from the Federal Reserve's calendar page on 2026-10-26 (FED_PAGE): the second
# day of every meeting, 2021-2027. 2027 is "tentative until confirmed".
FED_DECISION_DAYS = {
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


def _meetings() -> list[dict[str, Any]]:
    return list(yaml.safe_load(FOMC.read_text())["meetings"])


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
    assert (fomc.valid_from, fomc.valid_through) == (date(2021, 1, 1), date(2027, 12, 31))
    expected = [
        date.fromisoformat(f"{year}-{md}")
        for year, days in FED_DECISION_DAYS.items()
        for md in days
    ]
    assert [m.decision_day for m in fomc.meetings] == expected
    for m in fomc.meetings:
        assert (m.decision_day - m.first_day).days == 1  # two-day meetings
        # 2027 is tentative on the Fed's page: UNVERIFIED (a whole-day blackout)
        assert m.verified is (m.decision_day.year < 2027)


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
    extra = {"first_day": date(2025, 8, 21), "decision_day": date(2025, 8, 22), "verified": True}
    meetings = sorted([*_meetings(), extra], key=lambda m: m["decision_day"])
    with pytest.raises(ConfigError, match="2025 lists 9 meetings, not 8"):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))


def test_a_partial_year_is_not_counted(tmp_path: Path) -> None:
    """valid_through inside a year: that year cannot be checked for completeness."""
    meetings = [m for m in _meetings() if m["decision_day"] <= date(2027, 6, 30)]
    fomc = load_fomc(_write(tmp_path, FOMC, valid_through=date(2027, 6, 30), meetings=meetings))
    assert fomc.meetings[-1].decision_day == date(2027, 6, 9)


def test_partial_first_year_is_not_counted(tmp_path: Path) -> None:
    """valid_from after 1 January: that year cannot be checked (seven meetings left)."""
    meetings = [m for m in _meetings() if m["decision_day"] >= date(2021, 2, 1)]
    fomc = load_fomc(_write(tmp_path, FOMC, valid_from=date(2021, 2, 1), meetings=meetings))
    assert fomc.meetings[0].decision_day == date(2021, 3, 17)


@pytest.mark.parametrize(
    ("index", "meeting", "match"),
    [
        (
            0,
            {"first_day": date(2021, 1, 28), "decision_day": date(2021, 1, 27), "verified": True},
            "first_day after its decision_day",
        ),
        (
            0,
            {"first_day": date(2020, 12, 15), "decision_day": date(2020, 12, 16), "verified": True},
            "outside valid_from",
        ),
        (
            -1,
            {"first_day": date(2028, 1, 25), "decision_day": date(2028, 1, 26), "verified": False},
            "outside valid_from",
        ),
        (
            1,
            {"first_day": date(2021, 1, 26), "decision_day": date(2021, 1, 27), "verified": True},
            "in date order",
        ),
        (
            0,
            {"first_day": date(2021, 1, 26), "decision_day": date(2021, 1, 27), "verified": "yes"},
            "true or false",
        ),
        (0, {"first_day": date(2021, 1, 26), "decision_day": date(2021, 1, 27)}, "missing keys"),
        (0, "2021-01-27", "expected a mapping"),
    ],
)
def test_bad_meetings_are_refused(tmp_path: Path, index: int, meeting: object, match: str) -> None:
    meetings: list[object] = list(_meetings())
    meetings[index] = meeting
    with pytest.raises(ConfigError, match=match):
        load_fomc(_write(tmp_path, FOMC, meetings=meetings))
