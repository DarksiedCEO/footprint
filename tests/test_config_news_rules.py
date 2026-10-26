"""config/news.yaml (Andre's N1-N4 and the N-OPEN defaults, 2026-10-14): loading and refusals."""

from datetime import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from footprint.core.config import ConfigError, load_news_rules, load_profiles
from tests.conftest import REPO_ROOT

NEWS = REPO_ROOT / "config" / "news.yaml"


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
    assert [(e.name, e.at) for e in rules.daily_events] == [
        ("8:30 ET releases (N1)", time(8, 30)),
        ("cash open (N3)", time(9, 30)),
        ("10:00 ET releases (N1)", time(10, 0)),
    ]
    assert rules.pre_news_until == "10:00 ET releases (N1)"
    assert rules.time_stop_after_daily is True
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
        ({"daily_events": [{"name": "a", "at": "08:30"}, {"name": "a", "at": "09:00"}]}, "twice"),
        ({"daily_events": [{"name": "a"}]}, "missing keys"),
        ({"daily_events": [{"name": "a", "at": "8h30"}]}, "must be HH:MM"),
        ({"daily_events": [{"name": " ", "at": "08:30"}]}, "non-empty string"),
        ({"pre_news_until": "no such event"}, "pre_news_until"),
        ({"time_stop_after_daily": "yes"}, "true or false"),
        ({"overnight_flat_from": "15:00:30"}, "no seconds"),
        ({"overnight_flat_from": 1500}, "non-empty string"),
        ({"surprise": 1}, "unknown keys"),
        ({"lag_minutes": ...}, "missing keys"),
    ],
)
def test_bad_news_rules_are_refused(tmp_path: Path, changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_news_rules(_write(tmp_path, NEWS, **changes))
