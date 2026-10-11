"""Q5.2 slice 2b-1: the stairstep numbers load from config/backtest.yaml (`rules:`),
the backtest-only file the live runner never reads.

The two APPROVED numbers (three steps, 50% giveback) and the two DEFAULTs (accepted by
Andre 2026-10-10 for backtest only) are read through the typed loader; anything else
is refused at load time, never at the first decision.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from footprint.backtest.settings import load_backtest
from footprint.backtest.stairstep import StairSettings
from footprint.core.config import ConfigError
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config" / "backtest.yaml"


def _raw() -> dict[str, object]:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _with_stair(tmp_path: Path, **changes: object) -> Path:
    raw = _raw()
    rules = raw["rules"]
    assert isinstance(rules, dict)
    stair = dict(rules["stairstep"])
    for key, value in changes.items():
        if value is None:
            del stair[key]
        else:
            stair[key] = value
    rules["stairstep"] = stair
    return _write(tmp_path, raw)


def test_the_approved_numbers_load_from_the_backtest_file() -> None:
    stair = load_backtest(CONFIG).rules.stairstep
    assert isinstance(stair, StairSettings)
    assert stair.steps == 3  # APPROVED BY ANDRE 2026-10-10: three consecutive valid steps
    assert stair.max_giveback == Decimal("0.5")  # APPROVED: at most 50% of its push


def test_the_defaults_are_marked_accepted_for_backtest_only() -> None:
    marker = "# DEFAULT: accepted by Andre 2026-10-10 for backtest; revisit after real-data runs"
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    for key in ("max_pause_bars", "max_stair_bars"):
        (line,) = [ln for ln in lines if ln.strip().startswith(f"{key}:")]
        assert marker in line, line
    stair = load_backtest(CONFIG).rules.stairstep
    assert stair.max_pause_bars >= 1
    assert stair.max_stair_bars >= 1


def test_the_values_reach_the_settings_unchanged(tmp_path: Path) -> None:
    path = _with_stair(tmp_path, steps=4, max_giveback=0.25, max_pause_bars=7, max_stair_bars=33)
    assert load_backtest(path).rules.stairstep == StairSettings(
        steps=4, max_giveback=Decimal("0.25"), max_pause_bars=7, max_stair_bars=33
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("steps", 0),
        ("steps", "three"),
        ("max_giveback", 0),
        ("max_giveback", -0.5),
        ("max_giveback", 1.01),
        ("max_giveback", "0.5"),  # G46-1: a quoted number is not a number
        ("max_giveback", True),
        ("max_pause_bars", 0),
        ("max_stair_bars", 0),
        ("max_stair_bars", 2.5),
    ],
)
def test_a_bad_value_is_refused_at_load(tmp_path: Path, key: str, value: object) -> None:
    with pytest.raises(ConfigError, match="stairstep"):
        load_backtest(_with_stair(tmp_path, **{key: value}))


def test_a_count_of_exactly_one_is_allowed(tmp_path: Path) -> None:
    # G46-1: the lower boundary of every count is one, inclusive.
    path = _with_stair(tmp_path, steps=1, max_pause_bars=1, max_stair_bars=1)
    stair = load_backtest(path).rules.stairstep
    assert (stair.steps, stair.max_pause_bars, stair.max_stair_bars) == (1, 1, 1)


def test_a_giveback_of_exactly_one_is_allowed(tmp_path: Path) -> None:
    assert load_backtest(_with_stair(tmp_path, max_giveback=1)).rules.stairstep.max_giveback == 1


@pytest.mark.parametrize("key", ["steps", "max_giveback", "max_pause_bars", "max_stair_bars"])
def test_a_missing_key_is_refused(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_backtest(_with_stair(tmp_path, **{key: None}))


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="min_volume"):
        load_backtest(_with_stair(tmp_path, min_volume=5))


def test_a_missing_rules_section_is_refused(tmp_path: Path) -> None:
    raw = _raw()
    del raw["rules"]
    with pytest.raises(ConfigError, match="rules"):
        load_backtest(_write(tmp_path, raw))


def test_an_unknown_rule_section_is_refused(tmp_path: Path) -> None:
    raw = _raw()
    rules = raw["rules"]
    assert isinstance(rules, dict)
    rules["staircase"] = {}
    with pytest.raises(ConfigError, match="staircase"):
        load_backtest(_write(tmp_path, raw))


def test_a_refusal_names_the_place_once(tmp_path: Path) -> None:
    for key, value in (("steps", 0), ("max_giveback", 1.5)):
        path = _with_stair(tmp_path, **{key: value})
        with pytest.raises(ConfigError) as err:
            load_backtest(path)
        assert str(err.value).count(str(path)) == 1, err.value
        assert str(err.value).startswith(f"{path}: rules: stairstep: ")
