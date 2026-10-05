"""The readout: metrics, the go-live gates, the held-out trades, the verdict."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from footprint.backtest.engine import BacktestRun
from footprint.backtest.readout import measure, read_out, to_json, to_markdown
from footprint.backtest.settings import BacktestSettings

D = Decimal
T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
BANK = D(25000)


def trades(pnls: list[int | str]) -> list[dict[str, Any]]:
    return [
        {
            "exit_ts": (T0 + timedelta(hours=i)).isoformat(),
            "net_pnl": str(p),
            "exit_reason": "STOP" if D(str(p)) < 0 else "CLOSE",
        }
        for i, p in enumerate(pnls)
    ]


def run_of(pnls: list[int | str], **kw: Any) -> BacktestRun:
    total = sum((D(str(p)) for p in pnls), D(0))
    return BacktestRun(
        T0,
        T0 + timedelta(days=30),
        BANK,
        BANK + total,
        trades(pnls),
        decisions=1000,
        fired=len(pnls),
        **kw,
    )


def test_metrics_by_hand() -> None:
    m = measure([D(1500), D(-1000), D(1500), D(-1000), D(-1000)], BANK)
    assert (m.trades, m.wins, m.losses) == (5, 2, 3)
    assert m.win_rate_pct == D(40)
    assert m.avg_win == D(1500)
    assert m.avg_loss == D(1000)
    assert m.payoff_ratio == D("1.5")
    assert m.profit_factor == D(3000) / D(3000)
    assert m.net_pnl == D(0)
    assert m.expectancy == D(0)
    # Equity 26500, 25500, 27000 (the peak), 26000, 25000: 2000 down from 27000.
    assert m.max_drawdown == D(2000)
    assert round(m.max_drawdown_pct, 4) == round(D(2000) / D(27000) * 100, 4)


def test_no_losses_means_no_payoff_or_profit_factor_not_infinity() -> None:
    m = measure([D(100), D(200)], BANK)
    assert m.payoff_ratio is None
    assert m.profit_factor is None
    assert measure([], BANK).win_rate_pct is None


def passing(n: int) -> list[int | str]:
    """A pattern that passes every gate: 50% wins, payoff 2, PF 2, tiny drawdown."""
    return [2000 if i % 2 == 0 else -1000 for i in range(n)]


def test_a_run_that_clears_every_gate_passes(settings: BacktestSettings) -> None:
    r = read_out(run_of(passing(240)), settings, {})
    assert r.verdict == "PASS", r.why
    assert r.held_out.trades == 72  # the last 30% by exit time
    assert r.in_sample.trades == 168


def test_too_few_trades_is_not_a_result(settings: BacktestSettings) -> None:
    r = read_out(run_of(passing(150)), settings, {})
    assert r.verdict == "NOT ENOUGH TRADES"


def test_the_held_out_trades_must_pass_too(settings: BacktestSettings) -> None:
    pnls = passing(170) + [-1000 if i % 3 else 1000 for i in range(80)]  # the end decays
    r = read_out(run_of(pnls), settings, {})
    assert r.verdict == "FAIL"
    assert any(why.startswith("out of sample") for why in r.why)
    whole = next(g for g in r.gates if g.segment == "all" and g.gate == "profit factor")
    assert whole.passed  # the whole run alone would have passed


def test_drawdown_past_the_gate_fails(settings: BacktestSettings) -> None:
    pnls = [-1000] * 5 + passing(240)  # 5,000 down from 25,000 at the start: 20%
    r = read_out(run_of(pnls), settings, {})
    assert r.whole.max_drawdown_pct > settings.gates.max_drawdown_pct
    assert r.verdict == "FAIL"


def test_a_ledger_that_disagrees_with_the_journal_is_invalid(settings: BacktestSettings) -> None:
    run = run_of(passing(240))
    run = replace(run, final_equity=run.final_equity + D("0.01"))  # one cent off
    r = read_out(run, settings, {})
    assert r.verdict == "INVALID"
    assert not r.ledger_matches_journal


def test_a_halted_run_is_invalid_and_a_latched_one_fails(settings: BacktestSettings) -> None:
    assert (
        read_out(run_of(passing(240), ended_early="halted: x"), settings, {}).verdict == "INVALID"
    )
    latched = run_of(
        passing(240), ended_early="kill switch latched (live, only Andre can clear it)"
    )
    assert read_out(latched, settings, {}).verdict == "FAIL"


def test_the_report_carries_the_verdict_gates_and_assumptions(settings: BacktestSettings) -> None:
    r = read_out(run_of(passing(240)), settings, {"option prices": "MODELLED"})
    text = to_markdown(r)
    assert "**Verdict: PASS**" in text
    assert "| out of sample | profit factor | >= 1.3 |" in text
    assert "option prices: MODELLED" in text
    assert to_json(r)["verdict"] == "PASS"
