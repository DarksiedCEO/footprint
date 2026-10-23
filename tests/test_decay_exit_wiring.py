"""Where Theta's decay exit (decision DX-1) runs: paper and the backtest, nowhere else.

Trade management itself does not know paper from live; the decay exit runs only where
it is handed one. The backtest engine hands it one. The live app hands it one only
after its hard-coded paper check (D33: a live account or port is refused before
anything is built), so it is paper only; going live is a code change that must
revisit DX-1. The live app cannot be imported here without IBKR's ibapi, so its
wiring is read from the source."""

from __future__ import annotations

import ast
from pathlib import Path

from footprint.agents.theta import DecayExit
from footprint.backtest.engine import Backtest
from footprint.backtest.rules import EXIT_RULES
from footprint.backtest.settings import load_backtest
from footprint.core.config import load_profiles
from footprint.data.file_source import CsvBarSource
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.conftest import REPO_ROOT

APP = REPO_ROOT / "src" / "footprint" / "live" / "app.py"


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
    ]


def _build() -> ast.FunctionDef:
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    [build] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build"]
    return build


def test_the_live_app_hands_the_manager_the_decay_exit_after_the_paper_check() -> None:
    build = _build()
    [paper] = _calls(build, "check_paper")
    [decay] = _calls(build, "DecayExit")
    [manager] = _calls(build, "Manager")
    assert paper.lineno < decay.lineno < manager.lineno
    keywords = {k.arg: k.value for k in manager.keywords}
    assert set(keywords) == {"decay_exit"}
    handed = keywords["decay_exit"]
    assert isinstance(handed, ast.Name)
    # The name handed on is the DecayExit just built, on the market data's chain.
    [assigned] = [
        n
        for n in ast.walk(build)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == handed.id for t in n.targets)
    ]
    assert assigned.value is decay
    chain = decay.args[-1]
    assert isinstance(chain, ast.Attribute)
    assert (ast.unparse(chain.value), chain.attr) == ("market", "chain")


def test_the_live_paper_check_comes_first_in_build() -> None:
    build = _build()
    [paper] = _calls(build, "check_paper")
    first_call = min(n.lineno for n in ast.walk(build) if isinstance(n, ast.Call))
    assert paper.lineno == first_call


def test_the_backtest_engine_hands_the_manager_a_decay_exit(tmp_path: Path) -> None:
    profiles = load_profiles(REPO_ROOT / "config", "nq.yaml")
    settings = load_backtest(REPO_ROOT / "config" / "backtest.yaml")
    csv = tmp_path / "bars.csv"
    write_bars(csv)
    bars = list(CsvBarSource(csv, profiles.instrument.symbol).bars())
    engine = Backtest(profiles, settings, bars, tmp_path / "run", trade_from=TRADE_FROM)
    watch = engine.runner._manager._decay_exit
    assert isinstance(watch, DecayExit)
    assert watch._chain == engine._chain


def test_a_backtest_with_a_named_exit_rule_keeps_the_decay_exit(tmp_path: Path) -> None:
    profiles = load_profiles(REPO_ROOT / "config", "nq.yaml")
    settings = load_backtest(REPO_ROOT / "config" / "backtest.yaml")
    csv = tmp_path / "bars.csv"
    write_bars(csv)
    bars = list(CsvBarSource(csv, profiles.instrument.symbol).bars())
    rule = EXIT_RULES["target"](profiles, settings.rules)
    engine = Backtest(
        profiles, settings, bars, tmp_path / "run", trade_from=TRADE_FROM, exit_rule=rule
    )
    manager = engine.runner._manager
    assert manager._exit_rule is rule
    assert isinstance(manager._decay_exit, DecayExit)
    assert manager._decay_exit._chain == engine._chain
