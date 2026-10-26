"""The command line: bars CSV in, readout out."""

import csv
import json
from decimal import Decimal
from pathlib import Path

from footprint.backtest.__main__ import main
from tests.backtest.conftest import TRADE_FROM, extend
from tests.conftest import REPO_ROOT
from tests.decision.scenario import market


def write_bars(path: Path) -> None:
    bars = extend(tuple(market()), [Decimal(-10)] * 15)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


def test_a_run_from_the_command_line_writes_its_readout(tmp_path: Path) -> None:
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    out = tmp_path / "run"
    code = main(
        [
            "--bars",
            str(bars),
            "--out",
            str(out),
            "--config",
            str(REPO_ROOT / "config"),
            "--trade-from",
            TRADE_FROM.isoformat(),
            "--rolls",
            "none",  # these bars have no contract roll (BH-9)
        ]
    )
    assert code == 1  # one trade: NOT ENOUGH TRADES
    readout = json.loads((out / "readout.json").read_text())
    assert readout["verdict"] == "NOT ENOUGH TRADES"
    assert readout["whole"]["trades"] == 1
    assert readout["ledger_matches_journal"] is True
    assert "MODELLED" in (out / "readout.md").read_text()
