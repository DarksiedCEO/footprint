"""Trade statistics and the go-live verdict for research runs (floats: research estimates).

- win: net > 0; loss: net < 0; a trade of exactly 0 is neither.
- profit factor = gross win / gross loss; payoff = average win / average loss. Both are
  None without a loss (never infinity) and payoff also without a win.
- max drawdown: the largest fall of the realized equity curve (start equity plus each
  trade's net, in trade order) from its running peak, in dollars and as a percentage of
  that peak.
- t statistic = mean / (population stdev / sqrt(n)), as the earlier research scripts;
  None with fewer than two trades or no spread.
- time parts: trades split by order into `parts` consecutive groups; part k starts at
  trade floor(k * n / parts), so the later parts take any remainder.

The verdict reads the gates from config/backtest.yaml (`gates`) with the backtest
readout's rules: the last out_of_sample_pct of the trades (rounded half to even) are
held out and the win rate, payoff, profit factor and drawdown gates must hold on them as
well (their drawdown measured from the equity at the split). Pass also needs at least
`min_positive_parts` of the `time_parts` parts with positive net.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from footprint.core.config import _PERCENT_MAX
from footprint.research.settings import GateSettings

_PCT = float(_PERCENT_MAX)


@dataclass(frozen=True, slots=True)
class Summary:
    n: int
    wins: int
    win_pct: float | None
    profit_factor: float | None
    payoff: float | None
    avg: float | None
    net: float
    max_drawdown: float
    max_drawdown_pct: float
    t_stat: float | None


def summarize(pnls: Sequence[float], start_equity: float) -> Summary:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    n = len(pnls)
    equity = peak = start_equity
    worst = worst_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - equity) / peak * _PCT)
    t_stat = None
    if pnls:  # one trade has stdev 0: no t statistic (the check below)
        mean = sum(pnls) / n
        sd = statistics.pstdev(pnls, mean)  # population stdev, as the old scripts
        if sd > 0:
            t_stat = mean / (sd / math.sqrt(n))
    return Summary(
        n=n,
        wins=len(wins),
        win_pct=len(wins) / n * _PCT if n else None,
        profit_factor=gross_win / gross_loss if losses else None,
        payoff=(gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else None,
        avg=sum(pnls) / n if n else None,
        net=sum(pnls),
        max_drawdown=worst,
        max_drawdown_pct=worst_pct,
        t_stat=t_stat,
    )


def thirds(pnls: Sequence[float], parts: int) -> list[float]:
    """Net of each of `parts` consecutive groups of trades (by order)."""
    edges = [k * len(pnls) // parts for k in range(parts)] + [len(pnls)]
    return [sum(pnls[edges[k] : edges[k + 1]]) for k in range(parts)]


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    checks: tuple[tuple[str, str, bool], ...]  # (gate, "actual vs required", passed)


def _at_least(value: float | None, floor: Decimal) -> tuple[str, bool]:
    if value is None:
        return f"n/a vs >= {floor}", False
    return f"{value:.4g} vs >= {floor}", value >= float(floor)


def judge(
    pnls: Sequence[float],
    start_equity: float,
    gates: GateSettings,
    time_parts: int,
    min_positive_parts: int,
) -> Verdict:
    n = len(pnls)
    held = int(
        (Decimal(n) * gates.out_of_sample_pct / _PERCENT_MAX).to_integral_value(ROUND_HALF_EVEN)
    )
    split = n - held
    whole = summarize(pnls, start_equity)
    oos = summarize(pnls[split:], start_equity + sum(pnls[:split]))
    checks: list[tuple[str, str, bool]] = [
        ("trades", f"{n} vs >= {gates.min_trades}", n >= gates.min_trades),
        (
            "out-of-sample trades",
            f"{oos.n} vs >= {gates.min_out_of_sample_trades}",
            oos.n >= gates.min_out_of_sample_trades,
        ),
    ]
    for prefix, s in (("", whole), ("oos ", oos)):
        for name, value, floor in (
            ("win rate %", s.win_pct, gates.min_win_rate_pct),
            ("payoff", s.payoff, gates.min_payoff_ratio),
            ("profit factor", s.profit_factor, gates.min_profit_factor),
        ):
            text, ok = _at_least(value, floor)
            checks.append((prefix + name, text, ok))
        checks.append(
            (
                prefix + "max drawdown %",
                f"{s.max_drawdown_pct:.4g} vs <= {gates.max_drawdown_pct}",
                s.max_drawdown_pct <= float(gates.max_drawdown_pct),
            )
        )
    positive = sum(1 for part in thirds(pnls, time_parts) if part > 0)
    checks.append(
        (
            "positive thirds",
            f"{positive} of {time_parts} vs >= {min_positive_parts}",
            positive >= min_positive_parts,
        )
    )
    return Verdict(all(ok for _, _, ok in checks), tuple(checks))
