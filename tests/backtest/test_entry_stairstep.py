"""Q5.2 slice 2b-2: "stairstep" and "stairstep_vol" are selectable entry rules.

The factory builds the stair trigger (slice 2a) from the rule numbers in
config/backtest.yaml (slice 2b-1) on the pipeline's 1-minute bars. Only the momentum
read changes; every other gate still decides (proved for any trigger by the seam
tests). "breakout" and the run with no rule named are unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.settings import BacktestSettings, RuleSettings
from footprint.backtest.stairstep import StairTrigger
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read
from tests.backtest.test_rules_seam import _run

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
    assert sorted(rules.ENTRY_RULES) == ["breakout", "stairstep", "stairstep_vol"]
    assert rules.EXIT_RULES == {}


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
