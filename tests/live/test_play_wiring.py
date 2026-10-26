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
        if isinstance(h.type, ast.Name) and h.type.id == "PlayRefused"
    ]
    assert len(handlers) == 1
    returns = [n for n in ast.walk(handlers[0]) if isinstance(n, ast.Return)]
    assert [ast.literal_eval(r.value) for r in returns if r.value is not None] == [2]


def _handler(name: str) -> tuple[ast.Try, ast.ExceptHandler]:
    main = _function(MAIN, "main")
    found = [
        (n, h)
        for n in ast.walk(main)
        if isinstance(n, ast.Try)
        for h in n.handlers
        if isinstance(h.type, ast.Name) and h.type.id == name
    ]
    assert len(found) == 1, name
    return found[0]


def _refuses(handler: ast.ExceptHandler) -> None:
    returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
    assert [ast.literal_eval(r.value) for r in returns if r.value is not None] == [2]
    texts = [
        n.value
        for n in ast.walk(handler)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert any(t.startswith("REFUSED: ") for t in texts)


def test_a_bad_config_is_refused_with_exit_code_2_not_a_traceback() -> None:
    """L4: load_profiles' ConfigError (a bad regime or play config included) is printed
    as REFUSED and exits 2. NOT VERIFIED by running main (it imports ibapi)."""
    block, handler = _handler("ConfigError")
    assert "load_profiles" in _calls(ast.Module(body=block.body, type_ignores=[]))
    _refuses(handler)


def test_every_play_refusal_at_build_is_refused_with_exit_code_2() -> None:
    block, handler = _handler("PlayRefused")
    assert "build" in _calls(ast.Module(body=block.body, type_ignores=[]))
    _refuses(handler)
