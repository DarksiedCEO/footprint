"""The results readout: a backtest run measured against the go-live gates.

Trades come from the system's own journal (net of fees, G15). The equity curve is
realized: the bankroll plus each closed trade's net P&L, in exit order. Drawdown is
the largest fall from a peak of that curve, as a percentage of the peak.

The last `out_of_sample_pct` of the trades (by exit time) are held out: the gates
must hold on them as well as on the whole run. Nothing in v1 has been tuned on this
data, but the split is reported from the first run so tuning can never quietly use
the held-out trades.

The verdict is INVALID if the ledger's equity and the journal's trades disagree by
even a cent: then the run proves nothing.

The play caller's labels are counted per trading day (one journal record a day, by
cause). Only a TREND day runs the entry, so a run with no TREND day says why in the
verdict's reasons (warm-up after calendar_from, too little data, late, ...) rather than
reading as a strategy that simply never fired.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from footprint.backtest.engine import BacktestRun
from footprint.backtest.settings import BacktestSettings, GateSettings
from footprint.core.config import _PERCENT_MAX
from footprint.decision.play import PlayCause

_CENT = Decimal("0.01")
_WARM_UP_NOTE = (
    " (warm_up: the regime reader's history would reach before calendar_from; those "
    "days can never be labelled, PC-3)"
)
_PCT = _PERCENT_MAX


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate_pct: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    payoff_ratio: Decimal | None  # avg win / avg loss; None without both
    profit_factor: Decimal | None  # gross win / gross loss; None without a loss
    net_pnl: Decimal
    expectancy: Decimal | None
    max_drawdown_pct: Decimal
    max_drawdown: Decimal


def measure(pnls: list[Decimal], start_equity: Decimal) -> Metrics:
    """Metrics of trades (net P&L, in exit order) starting from `start_equity`."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    equity = peak = start_equity
    worst = worst_pct = Decimal(0)
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - equity) / peak * _PCT)
    return Metrics(
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=Decimal(len(wins)) / len(pnls) * _PCT if pnls else None,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=avg_win / avg_loss if avg_win is not None and avg_loss else None,
        profit_factor=gross_win / gross_loss if gross_loss else None,
        net_pnl=gross_win - gross_loss,
        expectancy=(gross_win - gross_loss) / len(pnls) if pnls else None,
        max_drawdown_pct=worst_pct,
        max_drawdown=worst,
    )


@dataclass(frozen=True, slots=True)
class GateCheck:
    segment: str
    gate: str
    required: str
    actual: str
    passed: bool


def _at_least(segment: str, gate: str, value: Decimal | None, floor: Decimal) -> GateCheck:
    actual = "n/a" if value is None else _fmt(value)
    return GateCheck(segment, gate, f">= {floor}", actual, value is not None and value >= floor)


def check_gates(
    whole: Metrics, held_out: Metrics, gates: GateSettings, marked_drawdown_pct: Decimal
) -> list[GateCheck]:
    """The whole run's drawdown gate uses the worse of the realized curve and the
    curve marked to the bid at every step (the loss inside a trade counts too)."""
    checks: list[GateCheck] = []
    checks.append(
        GateCheck(
            "all",
            "trades",
            f">= {gates.min_trades}",
            str(whole.trades),
            whole.trades >= gates.min_trades,
        )
    )
    checks.append(
        GateCheck(
            "out of sample",
            "trades",
            f">= {gates.min_out_of_sample_trades}",
            str(held_out.trades),
            held_out.trades >= gates.min_out_of_sample_trades,
        )
    )
    for segment, m in (("all", whole), ("out of sample", held_out)):
        checks.append(_at_least(segment, "win rate %", m.win_rate_pct, gates.min_win_rate_pct))
        checks.append(_at_least(segment, "payoff ratio", m.payoff_ratio, gates.min_payoff_ratio))
        checks.append(_at_least(segment, "profit factor", m.profit_factor, gates.min_profit_factor))
        drawdown = m.max_drawdown_pct
        if segment == "all":
            drawdown = max(drawdown, marked_drawdown_pct)
        checks.append(
            GateCheck(
                segment,
                "max drawdown %",
                f"<= {gates.max_drawdown_pct}",
                _fmt(drawdown),
                drawdown <= gates.max_drawdown_pct,
            )
        )
    return checks


@dataclass(frozen=True, slots=True)
class Readout:
    verdict: str  # PASS, FAIL, NOT ENOUGH TRADES, INVALID
    why: tuple[str, ...]
    whole: Metrics
    in_sample: Metrics
    held_out: Metrics
    gates: tuple[GateCheck, ...]
    ledger_matches_journal: bool
    exit_reasons: dict[str, int]
    play_days: dict[str, int]  # trading days by the play caller's cause (one label a day)
    run: dict[str, Any]
    assumptions: dict[str, Any]


def read_out(run: BacktestRun, settings: BacktestSettings, assumptions: dict[str, Any]) -> Readout:
    trades = sorted(run.trades, key=lambda t: t["exit_ts"])
    pnls = [Decimal(t["net_pnl"]) for t in trades]
    split = len(pnls) - int(
        (Decimal(len(pnls)) * settings.gates.out_of_sample_pct / _PCT).to_integral_value(
            ROUND_HALF_EVEN
        )
    )
    whole = measure(pnls, run.bankroll)
    in_sample = measure(pnls[:split], run.bankroll)
    held_out = measure(pnls[split:], run.bankroll + sum(pnls[:split], Decimal(0)))
    gates = check_gates(whole, held_out, settings.gates, run.marked_max_drawdown_pct)
    matches = run.final_equity - run.bankroll == sum(pnls, Decimal(0))
    why: list[str] = []
    if not matches:
        verdict = "INVALID"
        why.append(
            f"the ledger's equity change ({run.final_equity - run.bankroll}) does not equal "
            f"the journal's trades ({sum(pnls, Decimal(0))})"
        )
    elif run.open_at_end:
        verdict = "INVALID"  # its loss is unknown, so nothing can be concluded
        why.append("a trade could not be closed when the data ended")
    elif run.ended_early is not None and run.ended_early.startswith("halted"):
        verdict = "INVALID"  # a halt in a backtest is a defect to find, not a result
        why.append(f"the run ended early: {run.ended_early}")
    elif run.ended_early is not None:
        verdict = "FAIL"  # the strategy itself drew down to the kill switch
        why.append(f"the run ended early: {run.ended_early}")
    elif not (gates[0].passed and gates[1].passed):
        verdict = "NOT ENOUGH TRADES"
        why += [
            f"{g.segment} {g.gate}: {g.actual} (need {g.required})"
            for g in gates[:2]
            if not g.passed
        ]
    elif all(g.passed for g in gates):
        verdict = "PASS"
    else:
        verdict = "FAIL"
        why += [
            f"{g.segment} {g.gate}: {g.actual} (need {g.required})" for g in gates if not g.passed
        ]
    play_days = dict(sorted(Counter(str(p["cause"]) for p in run.plays).items()))
    if play_days and not play_days.get(PlayCause.TREND.value):
        why.append(
            "no trading day was labelled TREND, so the entry never ran: "
            + ", ".join(f"{cause} {count}" for cause, count in play_days.items())
            + _WARM_UP_NOTE * (PlayCause.WARM_UP.value in play_days)
        )
    return Readout(
        verdict=verdict,
        why=tuple(why),
        whole=whole,
        in_sample=in_sample,
        held_out=held_out,
        gates=tuple(gates),
        ledger_matches_journal=matches,
        exit_reasons=dict(Counter(str(t["exit_reason"]) for t in trades)),
        play_days=play_days,
        run={
            "first_bar": run.first_bar.isoformat(),
            "last_bar": run.last_bar.isoformat(),
            "bankroll": str(run.bankroll),
            "final_equity": str(run.final_equity),
            "decisions": run.decisions,
            "fired": run.fired,
            "alarms": len(run.alarms),
            "ended_early": run.ended_early,
            "open_at_end": run.open_at_end,
            "skipped_minutes": run.skipped_minutes,
            "steps": run.steps,
            "marked_max_drawdown_pct": str(run.marked_max_drawdown_pct),
            "marked_max_drawdown": str(run.marked_max_drawdown),
            "worst_open_loss": str(run.worst_open_loss),
        },
        assumptions=assumptions,
    )


def to_json(readout: Readout) -> dict[str, Any]:
    def plain(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [plain(v) for v in value]
        return value

    return plain(asdict(readout))  # type: ignore[no-any-return]


def to_markdown(readout: Readout) -> str:
    r = readout
    lines = [
        "# Backtest readout",
        "",
        f"**Verdict: {r.verdict}**",
        "",
    ]
    lines += [f"- {reason}" for reason in r.why]
    lines += [
        "",
        f"Data: {r.run['first_bar']} to {r.run['last_bar']}. "
        f"Equity {r.run['bankroll']} to {r.run['final_equity']}. "
        f"Ledger and journal agree: {'yes' if r.ledger_matches_journal else 'NO'}.",
        f"Decisions: {r.run['decisions']} ({r.run['fired']} fired). "
        f"Alarms: {r.run['alarms']}. Ended early: {r.run['ended_early'] or 'no'}.",
        f"Drawdown marked to the bid: {_fmt(Decimal(r.run['marked_max_drawdown_pct']))}% "
        f"({_fmt(Decimal(r.run['marked_max_drawdown']))}). Worst open loss in any trade: "
        f"{_fmt(Decimal(r.run['worst_open_loss']))}.",
        "",
        "## Gates",
        "",
        "| Segment | Gate | Required | Actual | Result |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {g.segment} | {g.gate} | {g.required} | {g.actual} | {'pass' if g.passed else 'FAIL'} |"
        for g in r.gates
    ]
    lines += ["", "## Metrics", "", "| | All | In sample | Out of sample |", "|---|---|---|---|"]
    for name in (
        "trades",
        "wins",
        "losses",
        "win_rate_pct",
        "avg_win",
        "avg_loss",
        "payoff_ratio",
        "profit_factor",
        "net_pnl",
        "expectancy",
        "max_drawdown_pct",
        "max_drawdown",
    ):
        values = [getattr(m, name) for m in (r.whole, r.in_sample, r.held_out)]
        lines.append(f"| {name} | " + " | ".join(_fmt(v) for v in values) + " |")
    lines += ["", "## Exit reasons", ""]
    lines += [f"- {reason}: {count}" for reason, count in sorted(r.exit_reasons.items())]
    lines += ["", "## Play caller (one label per trading day; only TREND trades)", ""]
    lines += [f"- {cause}: {count}" for cause, count in r.play_days.items()]
    if not r.play_days:
        lines.append("- no day's label was journaled")
    lines += ["", "## Assumptions (read every result through these)", ""]
    lines += [f"- {key}: {value}" for key, value in r.assumptions.items()]
    return "\n".join(lines) + "\n"


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        return str(value.quantize(_CENT, ROUND_HALF_EVEN))
    return str(value)
