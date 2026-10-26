"""The live program runs the scheduled news windows (N1-N6) exactly as the backtest does.

live/app.py imports IBKR's ibapi, which this sandbox cannot install, so the wiring is
checked from the source (AST): NOT VERIFIED by running the live program here. The
windows themselves are tested in tests/decision/test_news_rules.py.
"""

import ast
from pathlib import Path

from footprint.execution.channels import CRITICAL_MARKERS
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


def test_live_daily_calendar_check_alarms_the_fomc_calendar() -> None:
    tree = _tree(APP)
    (check,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check_calendar"
    ]
    calls = {getattr(n.func, "id", None) for n in ast.walk(check) if isinstance(n, ast.Call)}
    assert {"calendar_warning", "fomc_warning"} <= calls


def test_an_expired_fomc_calendar_rings_the_phone() -> None:
    assert "FOMC CALENDAR EXPIRED" in CRITICAL_MARKERS


def test_the_backtest_refuses_a_span_outside_the_fomc_calendar() -> None:
    calls = [
        n
        for n in ast.walk(_tree(ENGINE))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "check_fomc_span"
    ]
    assert len(calls) == 1


def test_live_daily_calendar_check_sends_the_fomc_alarm() -> None:
    """check_calendar run on its own (lifted from the source: the module needs ibapi),
    with the shipped profiles, on a day past the FOMC calendar: the critical alarm goes
    out once that day."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from footprint.core.calendar import calendar_warning
    from footprint.core.config import load_profiles
    from footprint.decision.news_rules import fomc_warning
    from footprint.execution.channels import is_critical

    (check,) = [
        n
        for n in ast.walk(_tree(APP))
        if isinstance(n, ast.FunctionDef) and n.name == "check_calendar"
    ]
    namespace = {
        "calendar_warning": calendar_warning,
        "fomc_warning": fomc_warning,
        "ZoneInfo": ZoneInfo,
    }
    exec(compile(ast.Module([check], []), str(APP), "exec"), namespace)
    sent: list[str] = []
    live = SimpleNamespace(
        profiles=load_profiles(REPO_ROOT / "config", "nq.yaml"),
        clock=lambda: datetime(2028, 1, 5, 18, tzinfo=UTC),
        _calendar_checked=None,
        alarm=sent.append,
    )
    namespace["check_calendar"](live)  # type: ignore[operator]
    namespace["check_calendar"](live)  # type: ignore[operator]
    fed = [m for m in sent if m.startswith("FOMC CALENDAR EXPIRED on 2027-12-31")]
    assert len(fed) == 1
    assert is_critical(fed[0])
