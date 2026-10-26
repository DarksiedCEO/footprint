"""The speed work's equivalence runs (BH-11): fixed synthetic datasets, run through the
command line exactly as a user runs it, every output file hashed.

The reference hashes (tests/backtest/golden/speed/*.json) were written by the code
BEFORE any speed change (qa/speed/make_reference.py, on main 928ca71). A speed change
must leave every byte of every output the same: the journal (decisions, plays,
trades), the ledger, readout.md, readout.json and the printed readout. Only the temp
path and the random position-id suffix are normalised, as in the seam golden.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from footprint.backtest.__main__ import main
from footprint.backtest.settings import load_backtest
from footprint.core.config import load_profiles
from tests.backtest.conftest import config_copy, write_csv
from tests.backtest.speed_market import speed_market
from tests.conftest import REPO_ROOT

REFERENCE = Path(__file__).parent / "golden" / "speed"
_POSITION_SUFFIX = re.compile(rb'("position_id": "[0-9T]+-\d+-)[0-9a-f]{8}"')
_FIRST_DAY = date(2024, 3, 11)


@dataclass(frozen=True)
class Scenario:
    """One run: `sessions` synthetic sessions from 2024-03-11, traded from
    `trade_from`; `real_play` False runs the backtest tests' always-TREND stand-in;
    `extra` are more command-line arguments (a named rule)."""

    name: str
    sessions: int
    trade_from: str
    real_play: bool
    extra: tuple[str, ...] = ()
    seed: int = 11


# The normal pytest run: 63 sessions (the play caller's 61 fill), the last one traded.
FAST: tuple[Scenario, ...] = (
    Scenario("real_play_1day", 63, "2024-06-07T00:00:00+00:00", real_play=True),
    Scenario("trend_1day", 63, "2024-06-07T00:00:00+00:00", real_play=False),
    Scenario(  # a gateless rule: trades, closed by the live exit
        "bb_squeeze_1day",
        63,
        "2024-06-07T00:00:00+00:00",
        real_play=False,
        extra=("--entry", "bb_squeeze_fast"),
    ),
    Scenario(  # trades closed by a named exit rule
        "bb_bounce_time_1day",
        63,
        "2024-06-07T00:00:00+00:00",
        real_play=False,
        extra=("--entry", "bb_bounce_fast", "--exit", "time"),
    ),
)
# The real play caller labels both days TREND and a rule trades on them (review F2).
FAST += (
    Scenario(
        "real_play_bb_bounce_time_2day",
        64,
        "2024-06-07T00:00:00+00:00",
        real_play=True,
        extra=("--entry", "bb_bounce_fast", "--exit", "time"),
    ),
)
# The long run (FOOTPRINT_SPEED_LONG=1): 130 sessions, the last three traded.
LONG = (Scenario("real_play_3day", 130, "2024-09-11T00:00:00+00:00", real_play=True),)


def _normalised(data: bytes, tmp_path: Path) -> bytes:
    data = data.replace(str(tmp_path).encode(), b"<TMP>")
    return _POSITION_SUFFIX.sub(rb'\1<RAND>"', data)


def run_hashes(scenario: Scenario, tmp_path: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Run `scenario` from the command line in `tmp_path`: {output file: sha256}, the
    printed readout as "stdout.txt", and the exit code as "exit"; and a summary for the
    reader (decisions, fired, trades, plays: lines in the journal)."""
    profiles = load_profiles(REPO_ROOT / "config", "nq.yaml")
    bars = tmp_path / "bars.csv"
    write_csv(bars, speed_market(profiles.instrument, _FIRST_DAY, scenario.sessions, scenario.seed))
    # The shipped option model (reference_price, BH-2) and roll block (BH-9, no roll
    # in these bars: --rolls none).
    reference = load_backtest(REPO_ROOT / "config" / "backtest.yaml").option_model.reference_price
    out = tmp_path / "run"
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = main(
            [
                "--bars",
                str(bars),
                "--out",
                str(out),
                "--config",
                str(config_copy(tmp_path / "config", reference, block_rolls=True)),
                "--rolls",
                "none",
                "--trade-from",
                datetime.fromisoformat(scenario.trade_from).isoformat(),
                *scenario.extra,
            ]
        )
    hashes = {
        p.relative_to(out).as_posix(): hashlib.sha256(
            _normalised(p.read_bytes(), tmp_path)
        ).hexdigest()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }
    hashes["stdout.txt"] = hashlib.sha256(
        _normalised(printed.getvalue().encode(), tmp_path)
    ).hexdigest()
    hashes["exit"] = str(code)
    summary = {
        name: _lines(out / "journal" / f"{name}.jsonl") for name in ("decisions", "trades", "plays")
    }
    summary["fired"] = sum(
        1
        for line in (out / "journal" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if '"fired": true' in line
    )
    return hashes, summary


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
