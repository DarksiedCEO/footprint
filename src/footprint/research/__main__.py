"""Research CLI: value one rule's option trades with Black-76 and print the report.

    uv run python -m footprint.research --bars nq_1m.csv --vol VXN_History.csv \\
        --rule NAME [--exit booker|scale_out|adr_bracket] [--days-to-expiry D] \\
        [--cost DOLLARS] [--instrument FILE] [--config DIR]

`--cost` replaces the instrument's configured round trip cost for a sensitivity run (the
report says so). A rule with a pre-registered t bar (research.yaml judging.min_t_stat)
is judged with it.

The report gives every statistic for all trades and for each session (by entry time),
the verdict against the go-live gates, and the exact settings used. Every modelled
number is marked UNVERIFIED.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from footprint.core.config import ConfigError
from footprint.research.bars import load_days
from footprint.research.engine import Trade, run
from footprint.research.exits import KINDS
from footprint.research.rules import RULES, check_t_bars
from footprint.research.sessions import names
from footprint.research.settings import ResearchSettings, load_research
from footprint.research.stats import Summary, judge, marked_drawdown, ruined_at, summarize, thirds
from footprint.research.vol import load_vol, vol_matches

U = "UNVERIFIED"


_MONEY = ",.2f"  # format specs: dollars and ratios to the cent, percentages to a tenth
_PCT = ",.1f"


def _num(value: float | None, spec: str = _MONEY) -> str:
    return "n/a" if value is None else format(value, spec)


def _row(label: str, s: Summary, width: int) -> str:
    return (
        f"{label.ljust(width)} n={s.n} wins={s.wins} win%={_num(s.win_pct, _PCT)} "
        f"PF={_num(s.profit_factor)} payoff={_num(s.payoff)} avg={_num(s.avg)} "
        f"net={_num(s.net)} maxDD={_num(s.max_drawdown)} ({_num(s.max_drawdown_pct, _PCT)}%) "
        f"t={_num(s.t_stat)}"
    )


def report(
    trades: Sequence[Trade],
    skipped: int,
    settings: ResearchSettings,
    header: Sequence[str],
    *,
    min_t_stat: float | None = None,
) -> str:
    pnls = [t.net for t in trades]
    lines = [*header, "", f"trades: {len(trades)}   skipped (no usable vol): {skipped}", ""]
    labels = ("all", *names(settings.sessions))
    width = max(len(label) for label in labels) + len(" ")
    for label in labels:
        part = [t.net for t in trades if label in ("all", t.session)]
        lines.append(_row(label, summarize(part, settings.bankroll), width) + f"   [{U}: modelled]")
    j = settings.judging
    parts = thirds(pnls, j.time_parts)
    lines.append("")
    lines.append("time parts (by trade order): " + ", ".join(_num(p) for p in parts) + f" [{U}]")
    marks = [t.marks for t in trades]
    marked_dd, marked_pct = marked_drawdown(marks, settings.bankroll)
    lines.append(
        f"max drawdown marked to the model inside trades: {_num(marked_dd)} "
        f"({_num(marked_pct, _PCT)}%) [{U}]"
    )
    ruin = ruined_at(pnls, settings.bankroll)
    if ruin is not None:
        lines.append(
            f"RUIN: equity reached zero or below at trade {ruin} of {len(trades)} "
            "(later trades are still listed; the verdict fails)"
        )
    reasons: dict[str, int] = {}
    for t in trades:
        for r in t.reasons:
            reasons[r] = reasons.get(r, 0) + 1
    lines.append(
        "exit fills: " + (", ".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "none")
    )
    verdict = judge(
        pnls,
        settings.bankroll,
        settings.gates,
        j.time_parts,
        j.min_positive_parts,
        marks=marks,
        min_t_stat=min_t_stat,
    )
    lines.append("")
    lines.append(f"verdict: {'PASS' if verdict.passed else 'FAIL'} [{U}: modelled option prices]")
    for name, text, ok in verdict.checks:
        lines.append(f"  {'ok  ' if ok else 'FAIL'} {name}: {text}")
    lines.append("")
    lines.extend(LIMITS)
    return "\n".join(lines)


LIMITS = (
    "known limits:",
    "  - the bars must be UNADJUSTED (raw contract prices; ratio-adjusted is also fine):"
    " option values are rescaled by the entry price, so a difference (Panama)"
    " back-adjusted series distorts every result. Not checked by this program.",
    "  - the stop and lock are judged on the model mid, net of the whole modelled round"
    " trip; live judges the stop on the bid and its lock net of fees only"
    " (research/exits.py).",
    f"  - the option's implied vol is the index close; costs are a flat round trip [{U}].",
)


def settings_lines(
    settings: ResearchSettings, args: argparse.Namespace, configured_cost: float
) -> list[str]:
    o = settings.option
    e = settings.exits
    cost = f"cost: {settings.round_trip_per_contract} per contract round trip"
    if args.cost is not None:
        cost += f" (--cost; config {configured_cost})"
    b = e.adr_bracket
    bracket = (
        ""
        if b is None
        else f"; adr_bracket stop {b.stop_range_fraction} and target "
        f"{b.target_range_fraction} of the rule's range, contracts {b.contracts}"
    )
    t_bar = settings.judging.min_t_stat.get(args.rule)
    return [
        f"rule: {args.rule}"
        + ("" if args.variant is None else f" (variant {args.variant})")
        + f"   exit: {args.exit}   bars: {args.bars}   vol: {args.vol}",
        f"timezone: {settings.timezone}   session day rolls at {settings.roll:%H:%M}",
        "sessions: "
        + ", ".join(f"{n} {w.start:%H:%M}-{w.end:%H:%M}" for n, w in settings.sessions),
        f"option: moneyness {o.moneyness_points} pts, {o.days_to_expiry} days to expiry, "
        f"{o.days_per_year} days/year, rate {o.rate} [{U}], strike step {o.strike_step}, "
        f"point value {o.point_value}, reference price {o.reference_price}",
        f"vol index {settings.vol_index} (file {args.vol.name}): "
        f"CLOSE / {settings.vol_divisor} used as implied vol [{U}], "
        f"stale after {settings.vol_max_age_days} days",
        f"{cost} [{U}]",
        f"exits: stop {e.stop_dollars}/contract gross, lock from {e.lock_start_dollars} net "
        f"keeping {e.lock_fraction:.2f} of peak, first target {e.first_target_dollars} net; "
        f"contracts booker {e.booker_contracts}, scale_out {e.scale_out_contracts}{bracket}",
        f"bankroll {settings.bankroll}; gates from backtest.yaml; "
        f"pass also needs {settings.judging.min_positive_parts} of "
        f"{settings.judging.time_parts} time parts positive"
        + ("" if t_bar is None else f" and a t statistic of at least {t_bar}"),
        "t bar: "
        + ("none configured" if t_bar is None else f"{t_bar} (judging.min_t_stat.{args.rule})"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.research")
    parser.add_argument("--bars", type=Path, required=True, help="continuous 1-minute CSV")
    parser.add_argument("--vol", type=Path, required=True, help="Cboe volatility index CSV")
    parser.add_argument("--rule", required=True, choices=sorted(RULES))
    parser.add_argument("--variant", default=None, help="the rule's pre-registered variant")
    parser.add_argument("--exit", default=KINDS[0], choices=KINDS)
    parser.add_argument("--days-to-expiry", type=float, default=None)
    parser.add_argument("--cost", type=float, default=None, help="round trip per contract")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    args = parser.parse_args(argv)
    settings = load_research(args.config, args.instrument)
    try:
        check_t_bars(settings, args.rule)
    except ConfigError as exc:
        parser.error(str(exc))
    if not vol_matches(args.vol, settings.vol_index):
        parser.error(
            f"--vol {args.vol.name} is not the {settings.vol_index} history that "
            f"{args.instrument} names (research.yaml instruments)"
        )
    configured_cost = settings.round_trip_per_contract
    if args.cost is not None:
        if not (math.isfinite(args.cost) and args.cost >= 0):
            parser.error("--cost must be a finite number >= 0")
        settings = replace(settings, round_trip_per_contract=args.cost)
    if args.days_to_expiry is not None:
        if not args.days_to_expiry > 0:
            parser.error("--days-to-expiry must be > 0")
        settings = replace(
            settings, option=replace(settings.option, days_to_expiry=args.days_to_expiry)
        )
    registered = RULES[args.rule]
    variants = registered.variants(settings)
    if (args.variant is None) != (not variants) or (
        args.variant is not None and args.variant not in variants
    ):
        parser.error(
            f"--variant must be one of {variants} for {args.rule}"
            if variants
            else f"{args.rule} has no variants"
        )
    rule = registered.build(settings, args.variant)
    days = load_days(args.bars, settings.symbol, settings.zone, settings.roll)
    vols = load_vol(args.vol, settings.vol_divisor, settings.vol_max_age_days)
    trades, skipped = run(days, rule, vols, settings, args.exit)
    text = report(
        trades,
        skipped,
        settings,
        settings_lines(settings, args, configured_cost),
        min_t_stat=settings.judging.min_t_stat.get(args.rule),
    )
    extra = getattr(rule, "report", None)
    if callable(extra):
        text += "\n\nrule notes:\n" + "\n".join(extra(days))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
