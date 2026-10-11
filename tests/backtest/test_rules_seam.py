"""Q5.0 rules seam: the backtest can swap the entry trigger by name; nothing else moves.

The seam replaces only the momentum read (the trigger). Every other gate (trend
agreement, session, risk, kill switch, levels, sizing, the hard stop) still decides.
With no rule named, the run's output is byte-identical to the run before the seam
existed (the golden files were written by the pre-seam code at chain-recorder-v1
ca5399d; only the temp path and the random position-id suffix are normalised).
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from footprint.agents.momentum_reader import MomentumReader
from footprint.backtest import rules
from footprint.backtest.__main__ import main
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read, Score
from footprint.decision.pipeline import Decision, Pipeline
from footprint.decision.risk.gate import AccountState
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, chain_for, market
from tests.decision.test_console_pipeline import clean

GOLDEN = Path(__file__).parent / "golden" / "seam_default"
_POSITION_SUFFIX = re.compile(rb'("position_id": "[0-9T]+-\d+-)[0-9a-f]{8}"')


class FixedTrigger:
    """A stand-in rule: always reads `score`, says `reason`, and counts its calls."""

    def __init__(self, score: Score, reason: str) -> None:
        self.score = score
        self.reason = reason
        self.calls = 0

    def read(self, bars: Sequence[Bar]) -> Read:
        self.calls += 1
        return Read("momentum", bars[-1].ts, self.score, self.reason)


def _decide(
    profiles: Profiles,
    trigger: FixedTrigger | MomentumReader | None = None,
    account: AccountState | None = None,
) -> Decision:
    pipeline = Pipeline(profiles) if trigger is None else Pipeline(profiles, trigger)
    bars = market()
    return pipeline.decide(
        bars, chain_for(bars[-1].close), account or clean(profiles), (), DECISION_TIME
    )


def test_with_no_trigger_the_pipeline_decides_exactly_as_the_momentum_reader_does(
    profiles: Profiles,
) -> None:
    default = _decide(profiles)
    explicit = _decide(profiles, MomentumReader(profiles.strategy.volume, profiles.instrument))
    assert default.fired
    assert explicit.fired
    assert default.verdict == explicit.verdict
    assert default.plan == explicit.plan


def test_a_trigger_that_says_no_stops_the_entry_the_default_would_take(
    profiles: Profiles,
) -> None:
    trigger = FixedTrigger(Score.NEUTRAL, "rule says no")
    decision = _decide(profiles, trigger)
    assert trigger.calls == 1
    assert not decision.fired
    assert any("rule says no" in r for r in decision.reasons())


def test_a_trigger_against_the_trend_is_still_refused_by_the_trend_gate(
    profiles: Profiles,
) -> None:
    decision = _decide(profiles, FixedTrigger(Score.BEARISH, "rule says short-side"))
    assert not decision.fired
    assert any(r.startswith("momentum_with_trend") for r in decision.reasons())


def test_a_trigger_that_agrees_reaches_the_same_sized_entry_as_the_default(
    profiles: Profiles,
) -> None:
    default = _decide(profiles)
    swapped = _decide(profiles, FixedTrigger(Score.BULLISH, "rule fires"))
    assert swapped.fired
    assert swapped.plan == default.plan
    assert swapped.entry == default.entry


def test_the_risk_precheck_still_vetoes_a_firing_trigger(profiles: Profiles) -> None:
    held = replace(clean(profiles), position_open=True)
    decision = _decide(profiles, FixedTrigger(Score.BULLISH, "rule fires"), held)
    assert not decision.fired
    assert decision.precheck.vetoes


def _run(tmp_path: Path, *extra: str) -> tuple[Path, str]:
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    out = tmp_path / "run"
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        main(
            [
                "--bars",
                str(bars),
                "--out",
                str(out),
                "--config",
                str(REPO_ROOT / "config"),
                "--trade-from",
                TRADE_FROM.isoformat(),
                *extra,
            ]
        )
    return out, printed.getvalue()


def _normalised(data: bytes, tmp_path: Path) -> bytes:
    data = data.replace(str(tmp_path).encode(), b"<TMP>")
    return _POSITION_SUFFIX.sub(rb'\1<RAND>"', data)


def test_with_no_rule_named_the_run_is_byte_identical_to_before_the_seam(
    tmp_path: Path,
) -> None:
    out, printed = _run(tmp_path)
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    expected = sorted(p.relative_to(GOLDEN).as_posix() for p in GOLDEN.rglob("*") if p.is_file())
    assert written == [name for name in expected if name != "stdout.txt"]
    for name in written:
        assert _normalised((out / name).read_bytes(), tmp_path) == (GOLDEN / name).read_bytes(), (
            name
        )
    assert _normalised(printed.encode(), tmp_path) == (GOLDEN / "stdout.txt").read_bytes()


def test_a_named_entry_rule_replaces_the_trigger_for_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[FixedTrigger] = []

    def never(profiles: Profiles) -> FixedTrigger:
        built.append(FixedTrigger(Score.NEUTRAL, "never"))
        return built[-1]

    monkeypatch.setitem(rules.ENTRY_RULES, "never", never)
    out, printed = _run(tmp_path, "--entry", "never")
    assert len(built) == 1
    assert built[0].calls > 0
    assert '"trades": 0' in (out / "readout.json").read_text()
    assert "entry rule" in printed
    assert "never" in printed


def test_an_unknown_entry_rule_is_refused_before_anything_runs(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        _run(tmp_path, "--entry", "no-such-rule")
    assert refused.value.code == 2
    assert not (tmp_path / "run").exists()


class AlwaysOut:
    """A stand-in exit rule: out at the first fresh quote it is asked about."""

    def __init__(self) -> None:
        self.asked = 0

    def wants_out(self, position: object, net_open: object, now: object) -> bool:
        self.asked += 1
        return True


def test_a_named_exit_rule_is_asked_and_can_close_the_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[AlwaysOut] = []

    def always(profiles: Profiles) -> AlwaysOut:
        built.append(AlwaysOut())
        return built[-1]

    monkeypatch.setitem(rules.EXIT_RULES, "always", always)
    out, printed = _run(tmp_path, "--exit", "always")
    assert len(built) == 1
    assert built[0].asked > 0
    trades = (out / "journal" / "trades.jsonl").read_text()
    assert '"RULE"' in trades
    assert "exit rule" in printed
    assert "always" in printed


def test_an_unknown_exit_rule_is_refused_before_anything_runs(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        _run(tmp_path, "--exit", "no-such-rule")
    assert refused.value.code == 2
    assert not (tmp_path / "run").exists()


_LIVE_BUILT = ("Pipeline", "Manager")  # built by the live path; never handed a candidate
_BACKTEST_MODULE = re.compile(r"footprint\.backtest(\..*)?")


def _named(node: ast.AST) -> str | None:
    """`Pipeline` for `Pipeline` and for `module.Pipeline` (any of _LIVE_BUILT)."""
    if isinstance(node, ast.Name) and node.id in _LIVE_BUILT:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _LIVE_BUILT:
        return node.attr
    return None


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Every node inside a type annotation (where naming Pipeline/Manager builds nothing)."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        if isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            roots.append(node.returns)
    return {id(n) for root in roots for n in ast.walk(root)}


def _live_path_violations(source: str, *, owns_exit_rule: bool = False) -> list[str]:
    """What, in one module outside footprint.backtest, could reach the backtest rule
    library or hand the live path a candidate rule (empty: nothing). Besides the plain
    spelling it sees (G36-1, G37-2): a module path in a string (importlib, __import__),
    Pipeline/Manager reached through a module, spread arguments, the class handed on or
    bound to another name (functools.partial), and the rule set by name or attribute.
    `owns_exit_rule`: trade management itself, which keeps the rule it is given."""
    tree = ast.parse(source)
    in_annotation = _annotation_nodes(tree)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("footprint.backtest")
        ):
            found.append(f"imports {node.module}")
        if isinstance(node, ast.Import):
            found += [
                f"imports {a.name}" for a in node.names if a.name.startswith("footprint.backtest")
            ]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _BACKTEST_MODULE.fullmatch(node.value):
                found.append(f"names the module {node.value}")
            if node.value in ("exit_rule", "_exit_rule"):
                found.append(f"names {node.value}")
        if isinstance(node, ast.Call):
            built = _named(node.func)
            if built == "Pipeline" and (
                len(node.args) != 1 or isinstance(node.args[0], ast.Starred) or node.keywords
            ):
                found.append("Pipeline built with more than the profiles")
            if built is not None and any(k.arg is None for k in node.keywords):
                found.append(f"{built} built with spread keywords")
        if isinstance(node, ast.keyword) and node.arg == "exit_rule":
            found.append("passes exit_rule")
        named = _named(node)
        if (
            named is not None
            and isinstance(getattr(node, "ctx", None), ast.Load)
            and id(node) not in called
            and id(node) not in in_annotation
        ):
            found.append(f"{named} handed on instead of built")
        if isinstance(node, ast.Attribute) and node.attr == "_exit_rule" and not owns_exit_rule:
            found.append("touches _exit_rule")
    return found


def test_no_live_module_imports_the_backtest_rules() -> None:
    """Candidate rules are backtest-only: nothing outside footprint.backtest may import
    footprint.backtest (rules included), the live path builds Pipeline(profiles), and no
    module outside footprint.backtest passes a Manager an exit rule."""
    src = REPO_ROOT / "src" / "footprint"
    malik = src / "execution" / "management" / "malik.py"
    for path in src.rglob("*.py"):
        if path.is_relative_to(src / "backtest"):
            continue
        source = path.read_text(encoding="utf-8")
        assert _live_path_violations(source, owns_exit_rule=path == malik) == [], path


# H1 (G36-1, G37-2): the guard above must also see the indirect ways of reaching the
# rule library or handing the live path a candidate rule, not only the plain spelling.
_EVASIONS = {
    "starred Pipeline args": "Pipeline(*(p, t))\n",
    "Pipeline through a module": "pipeline.Pipeline(p, t)\n",
    "Pipeline with a keyword": "Pipeline(p, trigger=t)\n",
    "Pipeline handed to partial": "functools.partial(Pipeline, p, t)()\n",
    "Pipeline bound to another name": "make = Pipeline\nmake(p, t)\n",
    "importlib": "importlib.import_module('footprint.backtest.rules')\n",
    "__import__": "__import__('footprint.backtest')\n",
    "exit_rule through a dict": "Manager(p, b, s, l, h, q, **{'exit_rule': r})\n",
    "Manager with spread keywords": "Manager(p, b, s, l, h, q, **extra)\n",
    "Manager handed to partial": "functools.partial(Manager, p)(b, s, l, h, q)\n",
    "Manager through a module": "malik.Manager(p, b, s, l, h, q, **extra)\n",
    "setattr of the rule": "setattr(manager, '_exit_rule', r)\n",
    "assignment of the rule": "manager._exit_rule = r\n",
}


@pytest.mark.parametrize("source", list(_EVASIONS.values()), ids=list(_EVASIONS))
def test_the_guard_sees_indirect_ways_into_the_live_path(source: str) -> None:
    assert _live_path_violations(source) != []


_ALLOWED = {
    "the live pipeline": "pipeline = Pipeline(p)\n",
    "the live manager": "manager = Manager(p, tracked, stops, ledger, halt, book)\n",
    "an annotation": "def f(pipeline: Pipeline, manager: Manager) -> Pipeline: ...\n",
    "an annotated name": "x: Manager | None = None\n",
    "a docstring": "'''footprint.backtest is the backtest; exit_rule is its seam.'''\n",
}


@pytest.mark.parametrize("source", list(_ALLOWED.values()), ids=list(_ALLOWED))
def test_the_guard_passes_the_live_spellings(source: str) -> None:
    assert _live_path_violations(source) == []


def test_only_trade_management_itself_holds_the_rule() -> None:
    held = "self._exit_rule = exit_rule\nif self._exit_rule is not None: pass\n"
    assert _live_path_violations(held, owns_exit_rule=True) == []
    assert _live_path_violations(held) != []
