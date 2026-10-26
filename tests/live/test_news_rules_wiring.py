"""The live program runs the scheduled news windows (N1-N4) exactly as the backtest does.

live/app.py imports IBKR's ibapi, which this sandbox cannot install, so the wiring is
checked from the source (AST): NOT VERIFIED by running the live program here. The
windows themselves are tested in tests/decision/test_news_rules.py.
"""

import ast
from pathlib import Path

from tests.conftest import REPO_ROOT

APP = REPO_ROOT / "src" / "footprint" / "live" / "app.py"
ENGINE = REPO_ROOT / "src" / "footprint" / "backtest" / "engine.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _runner_blackouts(tree: ast.Module) -> list[ast.expr]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Runner":
            found += [k.value for k in node.keywords if k.arg == "blackouts"]
    return found


def _is_with_news_rules(value: ast.expr, profiles: str, extra: str) -> bool:
    return (
        isinstance(value, ast.Call)
        and getattr(value.func, "id", None) == "with_news_rules"
        and [getattr(a, "id", None) for a in value.args] == [profiles, extra]
        and not value.keywords
    )


def test_live_runner_gets_the_news_rules_on_top_of_the_csv() -> None:
    (value,) = _runner_blackouts(_tree(APP))
    assert _is_with_news_rules(value, "p", "blackouts"), ast.dump(value)


def test_backtest_runner_gets_the_news_rules_on_top_of_the_csv() -> None:
    (value,) = _runner_blackouts(_tree(ENGINE))
    assert _is_with_news_rules(value, "p", "blackouts"), ast.dump(value)
