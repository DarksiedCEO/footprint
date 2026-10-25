"""Run a backtest from the command line.

    python -m footprint.backtest --bars data/nq_1m.csv --out runs/first [--entry NAME] [--exit NAME]

Writes readout.md and readout.json (plus the run's ledger and journal) into --out,
which must not exist yet or be empty. Exit code: 0 PASS, 1 FAIL or NOT ENOUGH
TRADES, 2 INVALID (also: refused before it ran, because the option model cannot be
traded at the data's prices or no traded bar is in the prime session; see
backtest/model_check.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from footprint.backtest.engine import Backtest
from footprint.backtest.model_check import BacktestRefused
from footprint.backtest.readout import read_out, to_json, to_markdown
from footprint.backtest.rule_console import RuleConsole
from footprint.backtest.rules import (
    ENTRY_RULES,
    EXIT_RULES,
    GATELESS_ENTRY_RULES,
    entry_rule_numbers,
    exit_rule_numbers,
)
from footprint.backtest.settings import load_backtest
from footprint.core.config import load_profiles
from footprint.data.file_source import CsvBarSource
from footprint.data.news import blackouts_from

_INVALID = 2  # the exit code of a run that is not valid (and of a refused one)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.backtest")
    parser.add_argument("--bars", type=Path, required=True, help="1-minute bars CSV")
    parser.add_argument("--out", type=Path, required=True, help="empty output directory")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument("--blackouts", type=Path, default=None, help="news blackouts CSV")
    parser.add_argument(
        "--trade-from",
        type=datetime.fromisoformat,
        default=None,
        help="ISO-8601 with offset; earlier bars are look-back history only",
    )
    parser.add_argument(
        "--entry",
        choices=sorted(ENTRY_RULES),
        default=None,
        help="a named entry rule (backtest only); omitted: the live entry, unchanged",
    )
    parser.add_argument(
        "--exit",
        choices=sorted(EXIT_RULES),
        default=None,
        help="a named exit rule (backtest only); omitted: the live exit, unchanged",
    )
    args = parser.parse_args(argv)

    profiles = load_profiles(args.config, args.instrument)
    settings = load_backtest(args.config / "backtest.yaml")
    bars = list(CsvBarSource(args.bars, profiles.instrument.symbol).bars())
    try:
        backtest = Backtest(
            profiles,
            settings,
            bars,
            args.out,
            blackouts=blackouts_from(args.blackouts),
            trade_from=args.trade_from,
            trigger=None
            if args.entry is None
            else ENTRY_RULES[args.entry](profiles, settings.rules),
            exit_rule=None
            if args.exit is None
            else EXIT_RULES[args.exit](profiles, settings.rules),
            console=(
                RuleConsole(profiles.strategy.console, profiles.instrument)
                if args.entry in GATELESS_ENTRY_RULES
                else None
            ),
        )
    except BacktestRefused as refused:
        print(f"backtest: {refused}", file=sys.stderr)
        return _INVALID
    run = backtest.run()
    assumptions = {
        "option prices": "MODELLED from the futures price (no options history)",
        "delta (G4, fixed)": str(profiles.strategy.options.assumed_delta),
        **{f"option model {k}": str(v) for k, v in asdict(settings.option_model).items()},
        "fees per contract per side": str(profiles.risk.fee_per_contract_per_side),
        # The exit numbers this run ran with, from the profiles it loaded (G52-2).
        "risk.profit_target": str(profiles.risk.profit_target),
        "risk.trail_keep_pct": str(profiles.risk.trail_keep_pct),
        "risk.stop_loss": str(profiles.risk.stop_loss),
        **_price_level(settings.option_model.reference_price),
        "news blackouts": str(args.blackouts) if args.blackouts else "NONE supplied",
        "intrabar path": "adverse-first against an open trade",
        "bars": str(args.bars),
    }
    if args.entry is not None:
        assumptions["entry rule"] = args.entry
        if args.entry in GATELESS_ENTRY_RULES:
            assumptions["confirmers"] = (
                "OFF (rule console: prime session and close lead only; no trend, zero-day, "
                "levels, VWAP or score gate)"
            )
        assumptions.update(entry_rule_numbers(args.entry, settings.rules))
    if args.exit is not None:
        assumptions["exit rule"] = args.exit
        assumptions.update(exit_rule_numbers(args.exit, settings.rules))
    readout = read_out(run, settings, assumptions)
    (args.out / "readout.md").write_text(to_markdown(readout), encoding="utf-8")
    (args.out / "readout.json").write_text(json.dumps(to_json(readout), indent=2), encoding="utf-8")
    print(to_markdown(readout))
    return {"PASS": 0, "FAIL": 1, "NOT ENOUGH TRADES": 1}.get(readout.verdict, _INVALID)


def _price_level(reference: Decimal | None) -> dict[str, str]:
    """BH-2: say how option prices were put at one level (nothing when they were not)."""
    if reference is None:
        return {}
    return {
        "price level": (
            f"options modelled as if the future traded at {reference}: each trade's futures "
            f"moves rescaled by {reference} / (the futures price its entry was decided at); "
            "strikes listed at that level; the bars the readers see NOT rescaled; the levels "
            "room N converted to the data's level"
        )
    }


if __name__ == "__main__":
    sys.exit(main())
