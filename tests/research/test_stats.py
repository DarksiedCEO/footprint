import math
from dataclasses import replace
from decimal import Decimal

import pytest

from footprint.research.settings import GateSettings
from footprint.research.stats import judge, summarize, thirds

GATES = GateSettings(
    min_trades=4,
    min_win_rate_pct=Decimal(45),
    min_payoff_ratio=Decimal("1.5"),
    min_profit_factor=Decimal("1.3"),
    max_drawdown_pct=Decimal(15),
    out_of_sample_pct=Decimal(30),
    min_out_of_sample_trades=1,
)


def test_zero_trades() -> None:
    s = summarize([], 25000.0)
    assert (s.n, s.wins, s.net, s.max_drawdown) == (0, 0, 0.0, 0.0)
    assert s.win_pct is None
    assert s.profit_factor is None
    assert s.payoff is None
    assert s.avg is None
    assert s.t_stat is None
    assert s.max_drawdown_pct == 0.0


def test_one_trade() -> None:
    s = summarize([300.0], 25000.0)
    assert (s.n, s.wins, s.net, s.avg) == (1, 1, 300.0, 300.0)
    assert s.win_pct == 100.0
    assert s.profit_factor is None  # no losses: not infinity
    assert s.payoff is None
    assert s.t_stat is None  # one trade has no spread


def test_no_losses_profit_factor_is_none_not_inf() -> None:
    s = summarize([100.0, 200.0], 25000.0)
    assert s.profit_factor is None
    assert s.payoff is None
    assert s.t_stat is not None
    assert s.t_stat == pytest.approx(150 / (50 / math.sqrt(2)))


def test_all_equal_trades_have_no_t_stat() -> None:
    assert summarize([100.0, 100.0], 25000.0).t_stat is None


def test_full_metrics() -> None:
    pnls = [500.0, -200.0, 300.0, -400.0, 100.0]
    s = summarize(pnls, 1000.0)
    assert s.n == 5
    assert s.wins == 3
    assert s.win_pct == pytest.approx(60.0)
    assert s.net == pytest.approx(300.0)
    assert s.avg == pytest.approx(60.0)
    assert s.profit_factor == pytest.approx(900 / 600)
    assert s.payoff == pytest.approx((900 / 3) / (600 / 2))
    # equity 1000 -> 1500 -> 1300 -> 1600 -> 1200 -> 1300: worst fall 400 from 1600
    assert s.max_drawdown == pytest.approx(400.0)
    assert s.max_drawdown_pct == pytest.approx(25.0)
    mean = 60.0
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / 5)  # population stdev
    assert s.t_stat == pytest.approx(mean / (sd / math.sqrt(5)))


def test_drawdown_from_the_start() -> None:
    s = summarize([-100.0, -50.0, 400.0], 1000.0)
    assert s.max_drawdown == pytest.approx(150.0)
    assert s.max_drawdown_pct == pytest.approx(15.0)


def test_a_zero_trade_is_not_a_win_nor_a_loss() -> None:
    s = summarize([0.0, 100.0, -50.0], 25000.0)
    assert s.wins == 1
    assert s.profit_factor == pytest.approx(2.0)
    assert s.payoff == pytest.approx(2.0)


def test_thirds_by_trade_order() -> None:
    assert thirds([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 3) == [3.0, 7.0, 11.0]
    assert thirds([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0], 3) == [3.0, 7.0, 18.0]
    assert thirds([1.0, -1.0], 3) == [0.0, 1.0, -1.0]
    assert thirds([], 3) == [0.0, 0.0, 0.0]
    assert thirds([1.0, 2.0, 3.0, 4.0], 2) == [3.0, 7.0]


def _passing() -> list[float]:
    # Last 3 (held out): 1000, -300, 1000. Thirds: 1700, 1700, 2700.
    return [1000.0, -300.0, 1000.0, 1000.0, -300.0, 1000.0, 1000.0, 1000.0, -300.0, 1000.0]


def test_judge_passes_when_every_gate_and_the_thirds_hold() -> None:
    v = judge(_passing(), 25000.0, GATES, time_parts=3, min_positive_parts=2)
    assert v.passed
    assert all(ok for _, _, ok in v.checks)
    names = [name for name, _, _ in v.checks]
    assert names == [
        "trades",
        "out-of-sample trades",
        "win rate %",
        "payoff",
        "profit factor",
        "max drawdown %",
        "oos win rate %",
        "oos payoff",
        "oos profit factor",
        "oos max drawdown %",
        "positive thirds",
    ]


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        ({"min_trades": 11}, {"trades"}),
        ({"min_out_of_sample_trades": 4}, {"out-of-sample trades"}),
        ({"min_win_rate_pct": Decimal(68)}, {"oos win rate %"}),  # 70 vs 66.7
        ({"min_win_rate_pct": Decimal(71)}, {"win rate %", "oos win rate %"}),
        ({"min_payoff_ratio": Decimal("3.4")}, {"payoff", "oos payoff"}),
        ({"min_profit_factor": Decimal(7)}, {"oos profit factor"}),  # 7.78 vs 6.67
        ({"min_profit_factor": Decimal(8)}, {"profit factor", "oos profit factor"}),
        # whole: 300 from 26,000 = 1.15%; held out: 300 from 30,400 = 0.99%
        ({"max_drawdown_pct": Decimal("1.1")}, {"max drawdown %"}),
        ({"max_drawdown_pct": Decimal("0.9")}, {"max drawdown %", "oos max drawdown %"}),
    ],
)
def test_each_gate_fails_on_its_own_number(change: dict[str, object], failing: set[str]) -> None:
    gates = replace(GATES, **change)  # type: ignore[arg-type]
    v = judge(_passing(), 25000.0, gates, time_parts=3, min_positive_parts=2)
    assert not v.passed
    assert {name for name, _, ok in v.checks if not ok} == failing


def test_gate_boundaries_are_inclusive() -> None:
    pnls = _passing()
    s = summarize(pnls, 25000.0)
    assert s.win_pct is not None
    assert s.payoff is not None
    assert s.profit_factor is not None
    exact = replace(
        GATES,
        min_trades=10,
        min_out_of_sample_trades=3,
        min_win_rate_pct=Decimal(str(s.win_pct)),
        min_payoff_ratio=Decimal(str(s.payoff)),
        min_profit_factor=Decimal(str(s.profit_factor)),
        max_drawdown_pct=Decimal(str(s.max_drawdown_pct)),
    )
    v = judge(pnls, 25000.0, exact, time_parts=3, min_positive_parts=2)
    assert [name for name, _, ok in v.checks if not ok if not name.startswith("oos")] == []


def test_out_of_sample_is_the_last_share_of_trades() -> None:
    # 10 trades, 30% held out: the last 3. They all lose: every oos gate fails.
    pnls = [1000.0] * 7 + [-100.0] * 3
    v = judge(pnls, 25000.0, GATES, time_parts=3, min_positive_parts=2)
    failing = {name for name, _, ok in v.checks if not ok}
    assert {"oos win rate %", "oos payoff", "oos profit factor"} <= failing
    assert "win rate %" not in failing


def test_out_of_sample_drawdown_starts_from_the_equity_at_the_split() -> None:
    pnls = [1000.0] * 7 + [-1000.0, 3000.0, 3000.0]
    v = judge(pnls, 1000.0, replace(GATES, max_drawdown_pct=Decimal(13)), 3, 2)
    oos = dict((n, ok) for n, _, ok in v.checks)
    assert oos["oos max drawdown %"] is True  # 1000 of 8000 = 12.5%
    assert oos["max drawdown %"] is True


def test_positive_thirds_rule() -> None:
    pnls = [-500.0] * 4 + _passing()
    v = judge(pnls, 25000.0, replace(GATES, min_trades=1), time_parts=3, min_positive_parts=3)
    assert dict((n, ok) for n, _, ok in v.checks)["positive thirds"] is False
    v2 = judge(pnls, 25000.0, replace(GATES, min_trades=1), time_parts=3, min_positive_parts=2)
    assert dict((n, ok) for n, _, ok in v2.checks)["positive thirds"] is True


def test_judge_with_no_trades_fails() -> None:
    v = judge([], 25000.0, GATES, time_parts=3, min_positive_parts=2)
    assert not v.passed
