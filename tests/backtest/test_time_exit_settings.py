"""Q5.6 slice 1: the time exit's numbers load from config/backtest.yaml (`rules: time:`),
the backtest-only file the live runner never reads.

Both numbers are APPROVED BY ANDRE 2026-10-10 ("if 10 minutes after the entry fill the
position's open profit is less than +$250, close the whole position"; "10 minutes and
$250 live in config (approved values)"). They are read through the typed loader;
anything else is refused at load time.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from footprint.backtest.exits import TimeExitSettings
from footprint.backtest.settings import load_backtest
from footprint.core.config import ConfigError
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config" / "backtest.yaml"
MARKER = "# APPROVED BY ANDRE 2026-10-10"


def _raw() -> dict[str, object]:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _with_time(tmp_path: Path, **changes: object) -> Path:
    raw = _raw()
    rules = raw["rules"]
    assert isinstance(rules, dict)
    time = dict(rules["time"])
    for key, value in changes.items():
        if value is None:
            del time[key]
        else:
            time[key] = value
    rules["time"] = time
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_the_approved_values_load_from_the_backtest_file_marked_approved() -> None:
    time = load_backtest(CONFIG).rules.time
    assert time == TimeExitSettings(minutes=10, min_gain=Decimal(250))
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    for key in ("minutes", "min_gain"):
        (line,) = [ln for ln in lines if ln.strip().startswith(f"{key}:")]
        assert MARKER in line, line


def test_the_values_reach_the_settings_unchanged(tmp_path: Path) -> None:
    settings = load_backtest(_with_time(tmp_path, minutes=7, min_gain=12.5))
    assert settings.rules.time == TimeExitSettings(minutes=7, min_gain=Decimal("12.5"))
    original = load_backtest(CONFIG).rules
    assert settings.rules.stairstep == original.stairstep
    assert settings.rules.pullback == original.pullback


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"minutes": 0}, "minutes"),
        ({"minutes": -1}, "minutes"),
        ({"minutes": 1.5}, "minutes"),
        ({"minutes": None}, "minutes"),
        ({"min_gain": 0}, "min_gain"),
        ({"min_gain": -250}, "min_gain"),
        ({"min_gain": None}, "min_gain"),
        ({"extra": 1}, "extra"),
    ],
)
def test_bad_values_are_refused_at_load(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_backtest(_with_time(tmp_path, **changes))


def test_one_minute_is_allowed(tmp_path: Path) -> None:
    assert load_backtest(_with_time(tmp_path, minutes=1)).rules.time.minutes == 1
