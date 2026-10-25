"""Run every named entry rule against every named exit rule (Q6, backtest only).

    python -m footprint.backtest.compare --bars data/nq_1m.csv --out runs/compare
        [--config C] [--instrument I] [--blackouts B] [--trade-from T]

Each pair runs through the single-run command itself (`python -m footprint.backtest
--entry E --exit X`, called, not copied) into DIR/<entry>__<exit>/ with that run's
normal files. Then DIR/summary.md and DIR/summary.csv list one row per pair, in sorted
pair order, with the verdict and every go-live gate's actual value and result COPIED
from that run's readout.json. Nothing is recomputed, ranked, sorted by result,
highlighted or recommended.

--out must not exist or be empty. If any run fails, the command stops non-zero naming
the pair; the completed runs stay in place and no summary is written. Exit code: 0
when every pair ran (whatever their verdicts), 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from footprint.backtest import __main__ as single
from footprint.backtest.rules import ENTRY_RULES, EXIT_RULES

NO_PROFIT_CLAIM = "Synthetic or unverified data says nothing about profitability."
SEPARATOR = "__"
_FAILED = 1


class CompareError(Exception):
    """The comparison cannot be completed; nothing is summarised."""


def pairs() -> list[tuple[str, str]]:
    """Every (entry, exit) name pair of the registries, in sorted order."""
    return [(entry, exit_) for entry in sorted(ENTRY_RULES) for exit_ in sorted(EXIT_RULES)]


def pair_dir(out: Path, entry: str, exit_: str) -> Path:
    return out / f"{entry}{SEPARATOR}{exit_}"


def _single_argv(args: argparse.Namespace, entry: str, exit_: str) -> list[str]:
    argv = [
        "--bars",
        str(args.bars),
        "--out",
        str(pair_dir(args.out, entry, exit_)),
        "--config",
        str(args.config),
        "--instrument",
        args.instrument,
    ]
    if args.blackouts is not None:
        argv += ["--blackouts", str(args.blackouts)]
    if args.trade_from is not None:
        argv += ["--trade-from", args.trade_from]
    if args.rolls is not None:
        argv += ["--rolls", args.rolls]
    return [*argv, "--entry", entry, "--exit", exit_]


def _run_pair(args: argparse.Namespace, entry: str, exit_: str) -> None:
    """One pair through the single-run command; its printed readout is the readout.md
    it writes, so it is not echoed. Raises CompareError if the run did not complete."""
    name = f"{entry}{SEPARATOR}{exit_}"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = single.main(_single_argv(args, entry, exit_))
    except SystemExit as stopped:
        raise CompareError(f"run {name} failed: exit {stopped.code}") from stopped
    except Exception as error:
        raise CompareError(f"run {name} failed: {type(error).__name__}: {error}") from error
    if not (pair_dir(args.out, entry, exit_) / "readout.json").is_file():
        raise CompareError(f"run {name} failed: exit {code}, no readout.json")


def _readout(out: Path, entry: str, exit_: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (pair_dir(out, entry, exit_) / "readout.json").read_text(encoding="utf-8")
    )
    return loaded


def _gate_names(readout: dict[str, Any]) -> list[str]:
    return [f"{g['segment']} {g['gate']}" for g in readout["gates"]]


def summarise(
    out: Path, bars: Path, config: Path, trade_from: str | None = None
) -> tuple[str, str]:
    """summary.md and summary.csv text from the pairs' readout.json files, values copied
    verbatim. Every run must cover the same bars and gates, or nothing is summarised."""
    readouts = [(entry, exit_, _readout(out, entry, exit_)) for entry, exit_ in pairs()]
    if not readouts:
        raise CompareError("no rule pairs to compare")
    first = readouts[0][-1]
    gates = _gate_names(first)
    span = (first["run"]["first_bar"], first["run"]["last_bar"])
    for entry, exit_, readout in readouts:
        name = f"{entry}{SEPARATOR}{exit_}"
        if _gate_names(readout) != gates:
            raise CompareError(f"run {name} has different gates; not summarised")
        if (readout["run"]["first_bar"], readout["run"]["last_bar"]) != span:
            raise CompareError(f"run {name} covers different bars; not summarised")

    header = ["entry", "exit", "verdict"]
    for gate in gates:
        header += [gate, f"{gate} result"]
    rows: list[list[str]] = []
    for entry, exit_, readout in readouts:
        row = [entry, exit_, str(readout["verdict"])]
        for g in readout["gates"]:
            row += [str(g["actual"]), "pass" if g["passed"] else "FAIL"]
        rows.append(row)

    table = io.StringIO()
    csv.writer(table, lineterminator="\n").writerows([header, *rows])

    lines = [
        "# Backtest comparison: every entry rule against every exit rule",
        "",
        f"**{NO_PROFIT_CLAIM}**",
        "",
        f"- bars: {bars}",
        f"- first bar: {span[0]}",
        f"- last bar: {span[1]}",
        f"- config: {config}",
        f"- trade from: {trade_from if trade_from is not None else 'the first bar'}",
        "",
        "Rows are in sorted pair order, not by result. Each value is copied from "
        "<entry>__<exit>/readout.json (its gates' actual values and results); read each "
        "run's readout.md for its assumptions and exit reasons.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n", table.getvalue()


def _iso_moment(text: str) -> str:
    """The text unchanged, once it parses as the single run will parse it; a bad value
    is refused before any run starts."""
    datetime.fromisoformat(text)
    return text


def _write(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.backtest.compare")
    parser.add_argument("--bars", type=Path, required=True, help="1-minute bars CSV")
    parser.add_argument("--out", type=Path, required=True, help="empty output directory")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument("--blackouts", type=Path, default=None, help="news blackouts CSV")
    parser.add_argument(
        "--trade-from",
        type=_iso_moment,
        default=None,
        help="passed to every run unchanged (see python -m footprint.backtest --help)",
    )
    parser.add_argument(
        "--rolls",
        default=None,
        help="passed to every run unchanged (see python -m footprint.backtest --help)",
    )
    args = parser.parse_args(argv)

    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        print(f"compare: refused: {args.out} exists and is not an empty directory", file=sys.stderr)
        return _FAILED
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        for entry, exit_ in pairs():
            _run_pair(args, entry, exit_)
        markdown, table = summarise(args.out, args.bars, args.config, args.trade_from)
    except CompareError as error:
        print(f"compare: {error}; completed runs are left in {args.out}", file=sys.stderr)
        return _FAILED
    _write(args.out / "summary.csv", table)
    _write(args.out / "summary.md", markdown)
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
