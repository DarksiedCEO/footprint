"""Review M1: every backtest readout records the decay-exit settings it ran with.

Theta's decay exit (DX-1) runs in the backtest whenever theta.decay_exit.enabled is
true, with or without a named exit rule, and it changes which trades close and how.
So readout.md, readout.json and the printed readout name theta.decay_exit.enabled,
window_minutes and max_pct_of_open_profit, taken from the profiles the run loaded:
research runs made with it on and with it off can always be told apart."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
from pathlib import Path

import pytest

from footprint.backtest.__main__ import main
from footprint.backtest.rules import EXIT_RULES
from footprint.core.config import load_profiles
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.conftest import REPO_ROOT

_KEYS = (
    "theta.decay_exit.enabled",
    "theta.decay_exit.window_minutes",
    "theta.decay_exit.max_pct_of_open_profit",
)


def _run(tmp_path: Path, config: Path, *extra: str) -> tuple[Path, str]:
    bars = tmp_path / "bars.csv"
    if not bars.exists():
        write_bars(bars)
    out = tmp_path / ("run" + "".join(extra).replace("-", "_"))
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        main(
            [
                "--bars",
                str(bars),
                "--out",
                str(out),
                "--config",
                str(config),
                "--trade-from",
                TRADE_FROM.isoformat(),
                *extra,
            ]
        )
    return out, printed.getvalue()


def _expected(config: Path) -> dict[str, str]:
    dx = load_profiles(config, "nq.yaml").strategy.theta.decay_exit
    return {
        "theta.decay_exit.enabled": "true" if dx.enabled else "false",
        "theta.decay_exit.window_minutes": str(dx.window_minutes),
        "theta.decay_exit.max_pct_of_open_profit": str(dx.max_pct_of_open_profit),
    }


def _check(out: Path, printed: str, expected: dict[str, str]) -> None:
    assumptions = json.loads((out / "readout.json").read_text())["assumptions"]
    markdown = (out / "readout.md").read_text()
    for key in _KEYS:
        assert assumptions[key] == expected[key], key
        assert f"\n- {key}: {expected[key]}\n" in markdown, key
        assert f"\n- {key}: {expected[key]}\n" in printed, key


def test_the_shipped_run_records_the_decay_exit_off(tmp_path: Path) -> None:
    config = REPO_ROOT / "config"
    out, printed = _run(tmp_path, config)
    expected = _expected(config)
    assert expected["theta.decay_exit.enabled"] == "false"  # off until DX-2 (review H1)
    _check(out, printed, expected)


@pytest.mark.parametrize("name", sorted(EXIT_RULES))
def test_every_named_exit_rule_records_the_decay_exit(tmp_path: Path, name: str) -> None:
    config = REPO_ROOT / "config"
    out, printed = _run(tmp_path, config, "--exit", name)
    _check(out, printed, _expected(config))


def _set(text: str, key: str, value: str) -> str:
    changed, count = re.subn(rf"(?m)^(\s+{key}:\s*)[^\s#]+", rf"\g<1>{value}", text)
    assert count == 1, key
    return changed


def test_the_settings_come_from_the_config_the_run_loaded(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    strategy = config / "strategy.yaml"
    shipped = load_profiles(REPO_ROOT / "config", "nq.yaml").strategy.theta.decay_exit
    text = strategy.read_text(encoding="utf-8")
    # Each moved inside its validated range, and away from the shipped value.
    text = _set(text, "enabled", "false" if shipped.enabled else "true")
    text = _set(text, "window_minutes", str(shipped.window_minutes + 1))
    text = _set(text, "max_pct_of_open_profit", str(shipped.max_pct_of_open_profit - 1))
    strategy.write_text(text, encoding="utf-8")
    expected = _expected(config)
    assert all(expected[k] != v for k, v in _expected(REPO_ROOT / "config").items())
    out, printed = _run(tmp_path, config)
    _check(out, printed, expected)


def test_the_decay_exit_lines_follow_the_exit_numbers(tmp_path: Path) -> None:
    out, _ = _run(tmp_path, REPO_ROOT / "config")
    keys = list(json.loads((out / "readout.json").read_text())["assumptions"])
    at = keys.index("risk.stop_loss")
    # Integration (cap10k-v1, #45): risk.position_cap sits right after the exit numbers
    # (its own test pins that); the decay-exit lines follow it, in this order.
    assert keys[at + 1] == "risk.position_cap"
    assert keys[at + 2 : at + 5] == list(_KEYS)
