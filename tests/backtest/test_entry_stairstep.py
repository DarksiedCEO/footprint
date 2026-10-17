"""Q5.2 slice 2b-2: "stairstep" and "stairstep_vol" are selectable entry rules.

The factory builds the stair trigger (slice 2a) from the rule numbers in
config/backtest.yaml (slice 2b-1) on the pipeline's 1-minute bars. Only the momentum
read changes; every other gate still decides (proved for any trigger by the seam
tests). "breakout" and the run with no rule named are unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.__main__ import main
from footprint.backtest.settings import BacktestSettings, RuleSettings
from footprint.backtest.stairstep import StairSettings, StairTrigger
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.backtest.test_rules_seam import _run
from tests.conftest import REPO_ROOT

ONE_MINUTE = timedelta(minutes=1)


@pytest.mark.parametrize(("name", "with_volume"), [("stairstep", False), ("stairstep_vol", True)])
def test_the_stair_rules_are_registered_and_built_from_the_backtest_file(
    profiles: Profiles, settings: BacktestSettings, name: str, with_volume: bool
) -> None:
    trigger = rules.ENTRY_RULES[name](profiles, settings.rules)
    assert type(trigger) is StairTrigger
    assert trigger._settings is settings.rules.stairstep
    assert trigger._interval == ONE_MINUTE
    assert trigger._with_volume is with_volume


def test_the_registered_rules_are_exactly_the_built_ones() -> None:
    bollinger = [f"bb_{s}_{v}" for s in ("bounce", "squeeze", "walk") for v in ("fast", "slow")]
    assert sorted(rules.ENTRY_RULES) == sorted(
        ["breakout", "pullback", "stairstep", "stairstep_vol", *bollinger]
    )
    assert sorted(rules.EXIT_RULES) == ["target", "time", "trail"]


@pytest.mark.parametrize("name", ["stairstep", "stairstep_vol"])
def test_a_stair_run_reads_every_decision_through_the_stair_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: BacktestSettings, name: str
) -> None:
    built: list[StairTrigger] = []
    seen: list[Read] = []
    real_factory = rules.ENTRY_RULES[name]
    real_read = StairTrigger.read

    def spy_factory(profiles: Profiles, rule_settings: RuleSettings) -> StairTrigger:
        assert rule_settings == settings.rules  # the numbers in config/backtest.yaml
        trigger = real_factory(profiles, rule_settings)
        assert isinstance(trigger, StairTrigger)
        built.append(trigger)
        return trigger

    def spy_read(self: StairTrigger, bars: tuple[Bar, ...]) -> Read:
        seen.append(real_read(self, bars))
        return seen[-1]

    monkeypatch.setitem(rules.ENTRY_RULES, name, spy_factory)
    monkeypatch.setattr(StairTrigger, "read", spy_read)
    out, printed = _run(tmp_path, "--entry", name)
    assert len(built) == 1
    assert seen
    assert all(r.reason.startswith(f"{name}: ") for r in seen)
    assert "entry rule" in printed
    assert name in printed
    assert (out / "readout.json").exists()


def _config_with_stair_numbers(tmp_path: Path) -> Path:
    """A copy of config/ whose stairstep numbers all differ from the repo's."""
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    path = config / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    for old, new in (
        ("    steps: 3 ", "    steps: 2 "),
        ("    max_giveback: 0.5 ", "    max_giveback: 0.4 "),
        ("    max_pause_bars: 10 ", "    max_pause_bars: 7 "),
        ("    max_stair_bars: 60 ", "    max_stair_bars: 33 "),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return config


@pytest.mark.parametrize("name", ["stairstep", "stairstep_vol"])
def test_a_stair_run_uses_and_records_the_numbers_of_the_named_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # G47-1: the readout names the stair numbers the run used; G47-2: they come from
    # --config, not from the repo's own config/.
    config = _config_with_stair_numbers(tmp_path)
    built: list[StairTrigger] = []
    real_factory = rules.ENTRY_RULES[name]

    def spy_factory(profiles: Profiles, rule_settings: RuleSettings) -> StairTrigger:
        trigger = real_factory(profiles, rule_settings)
        assert isinstance(trigger, StairTrigger)
        built.append(trigger)
        return trigger

    monkeypatch.setitem(rules.ENTRY_RULES, name, spy_factory)
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    out = tmp_path / "run"
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        main(
            [
                *("--bars", str(bars), "--out", str(out), "--config", str(config)),
                *("--trade-from", TRADE_FROM.isoformat(), "--entry", name),
            ]
        )
    assert [t._settings for t in built] == [StairSettings(2, Decimal("0.4"), 7, 33)]
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["entry rule"] == name
    assert {k: v for k, v in recorded.items() if k.startswith("rule ")} == {
        "rule stairstep steps": "2",
        "rule stairstep max_giveback": "0.4",
        "rule stairstep max_pause_bars": "7",
        "rule stairstep max_stair_bars": "33",
    }
    assert "- rule stairstep max_pause_bars: 7" in printed.getvalue()


def test_a_breakout_run_records_no_rule_numbers(tmp_path: Path) -> None:
    # breakout reads none of the rule library's numbers, so its readout names none.
    out, printed = _run(tmp_path, "--entry", "breakout")
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["entry rule"] == "breakout"
    assert not [k for k in recorded if k.startswith("rule ")]
    assert "rule stairstep" not in printed
    assert "rule pullback" not in printed
