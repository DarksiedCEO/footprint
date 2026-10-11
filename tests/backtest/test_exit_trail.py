"""Q5.4 exit "trail": the EXISTING exit, exposed under a name, unchanged.

Andre (2026-10-10): "= the EXISTING exit, unchanged: hard loss stop, +$1,000 then the
volume-aware trail (and scale-outs where contracts > 1). BASELINE. Expose it under the
name; change no logic." The existing exit is trade management with no exit rule added:
so "trail" adds none (its factory gives None) and the backtest builds trade management
exactly as the live runner does. The only difference a named run is allowed is the
readout's assumption line `exit rule: trail`. Synthetic bars say NOTHING about
profitability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.execution.management.malik import ExitRule, Manager
from tests.backtest.test_rules_seam import _normalised, _run


def test_trail_is_registered_and_adds_no_exit_rule(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    assert "trail" in rules.EXIT_RULES
    assert rules.EXIT_RULES["trail"](profiles, settings.rules) is None


def test_a_trail_run_builds_trade_management_exactly_as_the_live_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handed: list[ExitRule | None] = []
    real_manager = Manager

    def spy_manager(*args: object, **kwargs: object) -> object:
        handed.append(kwargs.get("exit_rule"))  # type: ignore[arg-type]
        assert "exit_rule" not in kwargs  # the live call: no exit rule passed at all
        return real_manager(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("footprint.backtest.engine.Manager", spy_manager)
    _run(tmp_path, "--exit", "trail")
    assert handed == [None]


def _without_exit_rule_line(text: str) -> str:
    kept = [line for line in text.splitlines(keepends=True) if "exit rule" not in line]
    assert len(kept) == len(text.splitlines(keepends=True)) - 1, "exactly one line differs"
    return "".join(kept)


def test_a_trail_run_writes_exactly_the_default_run_plus_the_rule_name(tmp_path: Path) -> None:
    default_dir, named_dir = tmp_path / "default", tmp_path / "named"
    default_dir.mkdir()
    named_dir.mkdir()
    default_out, default_printed = _run(default_dir)
    named_out, named_printed = _run(named_dir, "--exit", "trail")

    def files(out: Path) -> list[str]:
        return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())

    assert files(named_out) == files(default_out)
    # The scenario trades, so the entry and the exit are both compared.
    assert (default_out / "journal" / "trades.jsonl").read_text().strip()
    for name in files(default_out):
        want = _normalised((default_out / name).read_bytes(), default_dir)
        got = _normalised((named_out / name).read_bytes(), named_dir)
        if name == "readout.json":
            want_json, got_json = json.loads(want), json.loads(got)
            assert got_json["assumptions"].pop("exit rule") == "trail"
            assert "exit rule" not in want_json["assumptions"]
            assert not [k for k in got_json["assumptions"] if k.startswith("rule ")]
            assert got_json == want_json
        elif name == "readout.md":
            assert "- exit rule: trail" in got.decode()
            assert _without_exit_rule_line(got.decode()) == want.decode()
        else:
            assert got == want, name
    want_printed = _normalised(default_printed.encode(), default_dir).decode()
    got_printed = _normalised(named_printed.encode(), named_dir).decode()
    assert "exit rule" not in want_printed
    assert _without_exit_rule_line(got_printed) == want_printed


def test_trail_combines_with_a_named_entry_rule_unchanged(tmp_path: Path) -> None:
    entry_dir, both_dir = tmp_path / "entry", tmp_path / "both"
    entry_dir.mkdir()
    both_dir.mkdir()
    entry_out, _ = _run(entry_dir, "--entry", "breakout")
    both_out, _ = _run(both_dir, "--entry", "breakout", "--exit", "trail")
    want = _normalised((entry_out / "journal" / "trades.jsonl").read_bytes(), entry_dir)
    got = _normalised((both_out / "journal" / "trades.jsonl").read_bytes(), both_dir)
    assert want.strip()
    assert got == want
    recorded = json.loads((both_out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["entry rule"] == "breakout"
    assert recorded["exit rule"] == "trail"
