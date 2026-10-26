"""BH-11: the speed-up changes no result. Written before any speed change.

Each scenario runs the backtest from the command line on a fixed synthetic market and
hashes every output: the journal (decisions, plays, trades), the ledger, readout.md,
readout.json, the printed readout and the exit code. The reference hashes were written
by the code before the speed change (qa/speed/make_reference.py, on main 928ca71); a
speed change that moves any byte fails here. The long scenario (130 sessions, three
traded days) runs only with FOOTPRINT_SPEED_LONG=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.backtest.speed_runs import FAST, LONG, REFERENCE, Scenario, run_hashes


def _param(scenario: Scenario) -> object:
    marks = [pytest.mark.real_play] if scenario.real_play else []
    return pytest.param(scenario, id=scenario.name, marks=marks)


def _check(scenario: Scenario, tmp_path: Path) -> None:
    reference = json.loads((REFERENCE / f"{scenario.name}.json").read_text(encoding="utf-8"))
    hashes, summary = run_hashes(scenario, tmp_path)
    assert summary == reference["summary"]
    assert hashes == reference["hashes"]


@pytest.mark.parametrize("scenario", [_param(s) for s in FAST])
def test_every_output_byte_is_what_the_code_before_the_speed_up_wrote(
    scenario: Scenario, tmp_path: Path
) -> None:
    _check(scenario, tmp_path)


@pytest.mark.skipif(
    os.environ.get("FOOTPRINT_SPEED_LONG") != "1", reason="long run: FOOTPRINT_SPEED_LONG=1"
)
@pytest.mark.parametrize("scenario", [_param(s) for s in LONG])
def test_the_long_run_is_byte_identical_too(scenario: Scenario, tmp_path: Path) -> None:
    _check(scenario, tmp_path)


def test_the_references_cover_trades_plays_and_every_scenario() -> None:
    """The fixed runs exercise what the speed-up touches: decisions on every scenario,
    trades on some (the trade path), plays labelled by the real play caller."""
    records = [
        json.loads((REFERENCE / f"{s.name}.json").read_text(encoding="utf-8"))
        for s in (*FAST, *LONG)
    ]
    assert all(r["summary"]["decisions"] > 0 for r in records)
    assert sum(r["summary"]["trades"] for r in records) > 0
    assert sum(r["summary"]["plays"] for r in records) > 0
    # Review F2: the real play caller labels days TREND and a rule trades on them.
    real = [r for r, s in zip(records, (*FAST, *LONG), strict=True) if s.real_play]
    assert any(r["summary"]["trades"] > 0 for r in real)
