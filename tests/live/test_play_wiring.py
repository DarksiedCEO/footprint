"""Structural checks of the live wiring that run without IBKR's ibapi (which this
sandbox cannot install, so tests/live/test_live_app.py cannot run here): build() checks
the play caller's history before anything else is made, and the program turns that
refusal into exit code 2. The behaviour itself is tested in test_live_app.py."""

from __future__ import annotations

import ast

from tests.conftest import REPO_ROOT

APP = REPO_ROOT / "src" / "footprint" / "live" / "app.py"
MAIN = REPO_ROOT / "src" / "footprint" / "live" / "__main__.py"


def _function(path: object, name: str) -> ast.FunctionDef:
    tree = ast.parse(APP.read_text(encoding="utf-8") if path is APP else MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node: ast.AST) -> list[str]:
    return [
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]


def test_build_checks_the_play_history_right_after_the_paper_check() -> None:
    body = _function(APP, "build").body
    calls = [_calls(stmt) for stmt in body]
    flat = [c for stmt in calls for c in stmt]
    assert "check_live_history" in flat
    first_effect = flat.index("check_live_history")
    assert flat[:first_effect] == ["check_paper"]  # nothing is made before it
    assert "Pipeline" in flat  # the default play caller: no `play` argument
    for node in ast.walk(_function(APP, "build")):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Pipeline":
            assert len(node.args) == 1
            assert not node.keywords


def test_the_program_refuses_with_exit_code_2() -> None:
    main = _function(MAIN, "main")
    handlers = [
        h
        for n in ast.walk(main)
        if isinstance(n, ast.Try)
        for h in n.handlers
        if isinstance(h.type, ast.Name) and h.type.id == "PlayHistoryTooShort"
    ]
    assert len(handlers) == 1
    returns = [n for n in ast.walk(handlers[0]) if isinstance(n, ast.Return)]
    assert [ast.literal_eval(r.value) for r in returns if r.value is not None] == [2]
