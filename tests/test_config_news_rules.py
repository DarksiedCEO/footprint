"""config/news.yaml (Andre's N1-N4 and the N-OPEN defaults, 2026-10-14): loading and refusals."""

from datetime import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from footprint.core.config import ConfigError, load_news_rules, load_profiles
from tests.conftest import REPO_ROOT

NEWS = REPO_ROOT / "config" / "news.yaml"


def _event(name: str, at: str) -> dict[str, object]:
    return {"name": name, "at": at, "time_stop": True}


def _write(tmp_path: Path, source: Path, **changes: Any) -> Path:
    data = yaml.safe_load(source.read_text())
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    target = tmp_path / source.name
    target.write_text(yaml.safe_dump(data))
    return target


# --- the shipped files ------------------------------------------------------------


def test_the_shipped_news_rules_load() -> None:
    rules = load_news_rules(NEWS)
    assert rules.timezone == "America/New_York"
    assert (rules.lead_minutes, rules.lag_minutes) == (5, 2)
    assert [(e.name, e.at, e.time_stop) for e in rules.daily_events] == [
        ("8:30 ET releases (N1)", time(8, 30), True),
        ("cash open (N3)", time(9, 30), True),
        ("10:00 ET releases (N1)", time(10, 0), True),
    ]
    assert rules.pre_news_until == "10:00 ET releases (N1)"
    # N-OPEN-2 built default (PENDING Andre): every daily window carries the time stop.
    assert rules.pre_news_time_stop is True
    assert rules.overnight_flat_from == time(15, 0)


def test_profiles_carry_the_news_rules() -> None:
    p = load_profiles(REPO_ROOT / "config", "nq.yaml")
    assert p.news_rules == load_news_rules(NEWS)


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
        ({"daily_events": [_event("a", "08:30"), _event("a", "09:00")]}, "twice"),
        ({"daily_events": [{"name": "a", "time_stop": True}]}, "missing keys"),
        ({"daily_events": [{"name": "a", "at": "08:30"}]}, "missing keys"),
        ({"daily_events": [_event("a", "8h30")]}, "must be HH:MM"),
        ({"daily_events": [_event(" ", "08:30")]}, "non-empty string"),
        ({"daily_events": [{**_event("a", "08:30"), "time_stop": "yes"}]}, "true or false"),
        ({"pre_news_until": "no such event"}, "pre_news_until"),
        ({"pre_news_time_stop": "yes"}, "true or false"),
        ({"time_stop_after_daily": True}, "unknown keys"),  # replaced by the per-window switch
        ({"overnight_flat_from": "15:00:30"}, "no seconds"),
        ({"overnight_flat_from": 1500}, "non-empty string"),
        ({"surprise": 1}, "unknown keys"),
        ({"lag_minutes": ...}, "missing keys"),
    ],
)
def test_bad_news_rules_are_refused(tmp_path: Path, changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_news_rules(_write(tmp_path, NEWS, **changes))


# --- review round 1 M3: a window must be shorter than a day ------------------------


@pytest.mark.parametrize(("lead", "lag"), [(1440, 0), (0, 1440), (1000, 440), (2000, 5)])
def test_lead_and_lag_of_a_day_or_more_are_refused(tmp_path: Path, lead: int, lag: int) -> None:
    """windows() looks at the day before, of and after a moment only: a longer window
    could drop the time stop's look-back to an older one (fail open)."""
    with pytest.raises(ConfigError, match="shorter than one day"):
        load_news_rules(_write(tmp_path, NEWS, lead_minutes=lead, lag_minutes=lag))


def test_lead_and_lag_just_under_a_day_load(tmp_path: Path) -> None:
    rules = load_news_rules(_write(tmp_path, NEWS, lead_minutes=1000, lag_minutes=439))
    assert (rules.lead_minutes, rules.lag_minutes) == (1000, 439)


# --- review round 1 C2 / N-OPEN-2: the per-window switch ---------------------------


def test_the_narrow_n_open_2_reading_loads(tmp_path: Path) -> None:
    """Only after the 8:30 ET window: the other daily windows and N4 carry no time stop."""
    data = yaml.safe_load(NEWS.read_text())
    for event in data["daily_events"]:
        event["time_stop"] = event["name"] == "8:30 ET releases (N1)"
    data["pre_news_time_stop"] = False
    path = tmp_path / "news.yaml"
    path.write_text(yaml.safe_dump(data))
    rules = load_news_rules(path)
    assert [e.time_stop for e in rules.daily_events] == [True, False, False]
    assert rules.pre_news_time_stop is False
