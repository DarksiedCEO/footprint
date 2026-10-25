"""scripts/build_rolls.py and the committed roll file (backtest-history-v1, BH-9; review
round 2, R2-1). The roll sessions of the raw NQ front-month history, by the history
builder's own rule: session = the New York date of (ts + 6 h), the CME trade date; the
front contract of a session is the outright with the most volume in it; the front only
ever moves forward to a later-expiring contract. A roll session is the first session on
a new front contract."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from footprint.backtest.rolls import load_roll_sessions
from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "build_rolls.py"
ROLLS = REPO_ROOT / "data" / "rolls" / "nq_raw_rolls.csv"
ROLLS_SHA256 = "e6d86612552b0fd755e0196ac79d309b408ed36bede0aa24fa16702c8675c116"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_rolls", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEADER = "ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol\n"


def _row(ts: str, iid: int, volume: int, symbol: str) -> str:
    return f"{ts},33,1,{iid},1,1,1,1,{volume},{symbol}\n"


def _source(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "source.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


# A expires before B (its last bar is earlier). Sessions (trade dates): 06-08 A leads;
# 06-09 B leads (the roll); 06-10 A leads again by volume, but the front never goes back.
ROWS = [
    _row("2010-06-07T22:00:00.000000000Z", 1, 100, "NQM0"),  # 18:00 ET 06-07: session 06-08
    _row("2010-06-07T22:00:00.000000000Z", 2, 10, "NQU0"),
    _row("2010-06-08T21:59:00.000000000Z", 1, 500, "NQM0"),  # 17:59 ET 06-08: still 06-08
    _row("2010-06-08T22:00:00.000000000Z", 1, 10, "NQM0"),  # 18:00 ET 06-08: session 06-09
    _row("2010-06-08T22:00:00.000000000Z", 2, 100, "NQU0"),
    _row("2010-06-08T22:00:00.000000000Z", 3, 999, "NQM0-NQU0"),  # a spread: ignored
    _row("2010-06-09T22:00:00.000000000Z", 1, 900, "NQM0"),  # session 06-10: A leads again
    _row("2010-06-09T22:00:00.000000000Z", 2, 100, "NQU0"),
    _row("2010-06-10T22:00:00.000000000Z", 2, 1, "NQU0"),  # B trades on: it expires later
]


def test_the_builder_rule(tmp_path: Path) -> None:
    sessions = _script().roll_sessions(_source(tmp_path, ROWS))
    assert sessions == [date(2010, 6, 9)]  # once: the front never goes back to A


def test_spreads_are_not_contracts(tmp_path: Path) -> None:
    # A calendar spread leads session 06-08 and trades longest: read as a contract it
    # would be the front from the start, and B (expiring before it) could never take
    # over. Spreads (a "-" in the symbol) are skipped: the roll to B stays.
    rows = [
        *ROWS,
        _row("2010-06-07T22:00:00.000000000Z", 4, 9999, "NQM0-NQU0"),
        _row("2010-06-11T22:00:00.000000000Z", 4, 1, "NQM0-NQU0"),
    ]
    assert _script().roll_sessions(_source(tmp_path, rows)) == [date(2010, 6, 9)]


def test_the_script_writes_a_file_the_backtest_reads(tmp_path: Path) -> None:
    out = tmp_path / "rolls.csv"
    assert _script().main([str(_source(tmp_path, ROWS)), str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "session\n2010-06-09\n"
    assert load_roll_sessions(str(out)) == {date(2010, 6, 9)}


def test_the_script_refuses_an_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "rolls.csv"
    out.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        _script().main([str(_source(tmp_path, ROWS)), str(out)])
    assert out.read_text(encoding="utf-8") == "x"


def test_the_committed_roll_file_is_the_one_recorded() -> None:
    """Built from the Databento source (nq.csv.zst sha256 5ce4da84..., decompressed
    78582eba...) for nq_raw.csv (sha256 0e597f03...): docs/decisions-pending.md, BH-9."""
    assert hashlib.sha256(ROLLS.read_bytes()).hexdigest() == ROLLS_SHA256
    sessions = load_roll_sessions(str(ROLLS))
    assert len(sessions) == 58
    assert (min(sessions), max(sessions)) == (date(2010, 6, 11), date(2024, 9, 16))
    assert all(d.weekday() in (0, 4) for d in sessions)  # a Monday or a Friday session
