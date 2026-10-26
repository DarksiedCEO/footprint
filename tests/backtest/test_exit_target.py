"""Q5.5 exit "target": registered for the backtest, built from the configured target.

The behaviour inside trade management is pinned in tests/execution/test_exit_target_rule.py.
Synthetic bars say NOTHING about profitability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.exits import TargetExit
from footprint.core.config import Profiles
from footprint.execution.management.malik import ExitRule, Manager
from tests.backtest.test_rules_seam import _run


def test_target_is_registered_and_built_from_the_configured_target(profiles: Profiles) -> None:
    built = rules.EXIT_RULES["target"](profiles)
    assert isinstance(built, TargetExit)
    assert built.target is profiles.risk.profit_target


def test_a_target_run_hands_trade_management_the_rule_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handed: list[ExitRule | None] = []
    real_manager = Manager

    def spy_manager(*args: object, **kwargs: object) -> object:
        handed.append(kwargs.get("exit_rule"))  # type: ignore[arg-type]
        return real_manager(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("footprint.backtest.engine.Manager", spy_manager)
    out, printed = _run(tmp_path, "--exit", "target")
    assert len(handed) == 1
    assert isinstance(handed[0], TargetExit)
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["exit rule"] == "target"
    assert "- exit rule: target" in printed
