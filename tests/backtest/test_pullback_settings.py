"""Q5.3 slice 2a: the pullback setup lifetime loads from config/backtest.yaml (`rules:`),
the backtest-only file the live runner never reads.

The lifetime ("how many minutes after the spike the setup stays alive") is a DEFAULT
accepted by Andre 2026-10-10 for backtest only. It is read through the typed loader;
anything else is refused at load time, never at the first decision.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from footprint.backtest.pullback import PullbackSettings
from footprint.backtest.settings import load_backtest
from footprint.core.config import ConfigError
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config" / "backtest.yaml"
MARKER = "# DEFAULT: accepted by Andre 2026-10-10 for backtest; revisit after real-data runs"


def _raw() -> dict[str, object]:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _with_pullback(tmp_path: Path, **changes: object) -> Path:
    raw = _raw()
    rules = raw["rules"]
    assert isinstance(rules, dict)
    pullback = dict(rules["pullback"])
    for key, value in changes.items():
        if value is None:
            del pullback[key]
        else:
            pullback[key] = value
    rules["pullback"] = pullback
    return _write(tmp_path, raw)


def test_the_lifetime_loads_from_the_backtest_file_marked_as_a_default() -> None:
    pullback = load_backtest(CONFIG).rules.pullback
    assert isinstance(pullback, PullbackSettings)
    assert pullback.lifetime_minutes >= 1
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    (line,) = [ln for ln in lines if ln.strip().startswith("lifetime_minutes:")]
    assert MARKER in line, line


def test_the_value_reaches_the_settings_unchanged(tmp_path: Path) -> None:
    settings = load_backtest(_with_pullback(tmp_path, lifetime_minutes=7))
    assert settings.rules.pullback == PullbackSettings(lifetime_minutes=7)
    assert settings.rules.pullback.lifetime == timedelta(minutes=7)
    # the stair numbers are untouched by the new section
    assert settings.rules.stairstep == load_backtest(CONFIG).rules.stairstep


def test_a_lifetime_of_exactly_one_minute_is_allowed(tmp_path: Path) -> None:
    pullback = load_backtest(_with_pullback(tmp_path, lifetime_minutes=1)).rules.pullback
    assert pullback.lifetime == timedelta(minutes=1)


@pytest.mark.parametrize("value", [0, -1, "15", True, 1.5])
def test_a_bad_lifetime_is_refused_at_load(tmp_path: Path, value: object) -> None:
    path = _with_pullback(tmp_path, lifetime_minutes=value)
    with pytest.raises(ConfigError) as err:
        load_backtest(path)
    assert str(err.value).startswith(f"{path}: rules: pullback: "), err.value
    assert str(err.value).count(str(path)) == 1, err.value
    assert "lifetime_minutes" in str(err.value)


def test_a_missing_lifetime_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="lifetime_minutes"):
        load_backtest(_with_pullback(tmp_path, lifetime_minutes=None))


def test_an_unknown_pullback_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="min_volume"):
        load_backtest(_with_pullback(tmp_path, min_volume=5))


def test_a_missing_pullback_section_is_refused(tmp_path: Path) -> None:
    raw = _raw()
    rules = raw["rules"]
    assert isinstance(rules, dict)
    del rules["pullback"]
    with pytest.raises(ConfigError, match="pullback"):
        load_backtest(_write(tmp_path, raw))


@pytest.mark.parametrize("value", [0, -1])
def test_the_settings_refuse_a_lifetime_below_one_minute(value: int) -> None:
    with pytest.raises(ValueError, match="lifetime_minutes"):
        PullbackSettings(lifetime_minutes=value)
