"""Q5.6 slice 2: "time" is a selectable exit rule (`--exit time`).

The factory builds the time exit (slice 1) from the numbers in config/backtest.yaml
`rules: time:` (APPROVED BY ANDRE 2026-10-10: 10 minutes, +$250), read from the
`--config` the run names, never from the repo's own config/. The run records the rule
and both numbers in its readout. The behaviour inside trade management is pinned in
tests/execution/test_exit_time_rule.py. The run with no rule named, "trail" and
"target" are unchanged. Synthetic bars say NOTHING about profitability.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.__main__ import main
from footprint.backtest.exits import TimeExit, TimeExitSettings
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.execution.management.malik import ExitRule, Manager
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.backtest.test_rules_seam import _run
from tests.conftest import REPO_ROOT


def test_time_is_registered_and_built_from_the_backtest_file(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    built = rules.EXIT_RULES["time"](profiles, settings.rules)
    assert type(built) is TimeExit
    assert built._settings is settings.rules.time


def test_each_build_is_a_new_rule_with_no_verdicts(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    first = rules.EXIT_RULES["time"](profiles, settings.rules)
    second = rules.EXIT_RULES["time"](profiles, settings.rules)
    assert isinstance(first, TimeExit)
    assert isinstance(second, TimeExit)
    assert first is not second
    assert first._verdicts == {}
    assert first._verdicts is not second._verdicts


def test_only_time_names_rule_numbers(settings: BacktestSettings) -> None:
    assert rules.exit_rule_numbers("time", settings.rules) == {
        "rule time minutes": "10",
        "rule time min_gain": "250",
    }
    for name in ("trail", "target"):
        assert rules.exit_rule_numbers(name, settings.rules) == {}


def _config_with_time(tmp_path: Path) -> Path:
    """A copy of config/ whose time-exit numbers differ from the repo's."""
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    path = config / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    for old, new in (
        ("    minutes: 10 ", "    minutes: 7  "),
        ("    min_gain: 250 ", "    min_gain: 125.5 "),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return config


def test_a_time_run_uses_and_records_the_numbers_of_the_named_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_time(tmp_path)
    handed: list[ExitRule | None] = []
    real_manager = Manager

    def spy_manager(*args: object, **kwargs: object) -> object:
        handed.append(kwargs.get("exit_rule"))  # type: ignore[arg-type]
        return real_manager(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("footprint.backtest.engine.Manager", spy_manager)
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    out = tmp_path / "run"
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        main(
            [
                *("--bars", str(bars), "--out", str(out), "--config", str(config)),
                *("--trade-from", TRADE_FROM.isoformat(), "--exit", "time"),
            ]
        )
    assert len(handed) == 1  # one rule for the whole run: its verdicts live there
    (rule,) = handed
    assert type(rule) is TimeExit
    assert rule._settings == TimeExitSettings(minutes=7, min_gain=Decimal("125.5"))
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["exit rule"] == "time"
    assert {k: v for k, v in recorded.items() if k.startswith("rule ")} == {
        "rule time minutes": "7",
        "rule time min_gain": "125.5",
    }
    text = printed.getvalue()
    assert "- exit rule: time" in text
    assert "- rule time minutes: 7" in text
    assert "- rule time min_gain: 125.5" in text


@pytest.mark.parametrize("extra", [(), ("--exit", "trail"), ("--exit", "target")])
def test_other_runs_record_no_time_numbers(tmp_path: Path, extra: tuple[str, ...]) -> None:
    out, printed = _run(tmp_path, *extra)
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert not [k for k in recorded if k.startswith("rule ")]
    assert "rule time" not in printed
