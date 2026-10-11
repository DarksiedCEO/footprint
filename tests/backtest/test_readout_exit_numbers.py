"""Q6 (i), review G52-2: every backtest readout records the exit numbers it ran with.

readout.md and readout.json (the run with no rule named included) name
risk.profit_target, risk.trail_keep_pct and risk.stop_loss, taken from the profiles
the run loaded, so a run can always be traced to the exit numbers that produced it.
"""

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

_KEYS = ("risk.profit_target", "risk.trail_keep_pct", "risk.stop_loss")


def _run(tmp_path: Path, config: Path, *extra: str) -> Path:
    bars = tmp_path / "bars.csv"
    if not bars.exists():
        write_bars(bars)
    out = tmp_path / ("run" + "".join(extra).replace("-", "_"))
    with contextlib.redirect_stdout(io.StringIO()):
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
    return out


def _expected(config: Path) -> dict[str, str]:
    risk = load_profiles(config, "nq.yaml").risk
    return {
        "risk.profit_target": str(risk.profit_target),
        "risk.trail_keep_pct": str(risk.trail_keep_pct),
        "risk.stop_loss": str(risk.stop_loss),
    }


def _check(out: Path, expected: dict[str, str]) -> None:
    assumptions = json.loads((out / "readout.json").read_text())["assumptions"]
    markdown = (out / "readout.md").read_text()
    for key in _KEYS:
        assert assumptions[key] == expected[key], key
        assert f"\n- {key}: {expected[key]}\n" in markdown, key


def test_the_run_with_no_rule_named_records_its_exit_numbers(tmp_path: Path) -> None:
    config = REPO_ROOT / "config"
    _check(_run(tmp_path, config), _expected(config))


@pytest.mark.parametrize("name", sorted(EXIT_RULES))
def test_every_named_exit_rule_records_its_exit_numbers(tmp_path: Path, name: str) -> None:
    config = REPO_ROOT / "config"
    _check(_run(tmp_path, config, "--exit", name), _expected(config))


def _replace_number(text: str, key: str, value: str) -> str:
    changed, count = re.subn(rf"(?m)^{key}:\s*[0-9.]+", f"{key}: {value}", text)
    assert count == 1, key
    return changed


def test_the_numbers_come_from_the_config_the_run_loaded(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    risk = config / "risk.yaml"
    text = risk.read_text(encoding="utf-8")
    shipped = load_profiles(REPO_ROOT / "config", "nq.yaml").risk
    # Each moved inside its validated range, and away from the shipped value.
    text = _replace_number(text, "profit_target", str(shipped.profit_target + 1))
    text = _replace_number(text, "trail_keep_pct", str(shipped.trail_keep_pct - 1))
    text = _replace_number(text, "stop_loss", str(shipped.stop_loss + 1))
    risk.write_text(text, encoding="utf-8")
    expected = _expected(config)
    assert all(expected[k] != v for k, v in _expected(REPO_ROOT / "config").items())
    _check(_run(tmp_path, config), expected)


def test_the_exit_numbers_follow_the_fees_line_in_the_readout(tmp_path: Path) -> None:
    out = _run(tmp_path, REPO_ROOT / "config")
    keys = list(json.loads((out / "readout.json").read_text())["assumptions"])
    at = keys.index("fees per contract per side")
    assert keys[at + 1 : at + 4] == list(_KEYS)
