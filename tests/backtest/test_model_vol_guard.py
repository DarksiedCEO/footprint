"""The backtest refuses, loudly, to run where its option model cannot be traded
(greeks-veto-v1 review round 1, H1; Andre's decision GV-4 is still open).

The model prices the at-the-money option at a fixed `atm_premium_points` x root(days to
expiry), whatever the futures price, so the implied volatility of its quotes rises as
the price falls (about 1 / price). Below some price Vega (and lower still Theta) would
veto every entry; far enough above it Vega would refuse every quote as not believable.
A run over such prices would end with no trades and no error. Before it starts, the
backtest checks the model at the lowest and the highest price traded in the prime
session and refuses to run, naming the price, when either is outside the band.

Synthetic bars here prove the guard only: nothing about any rule's profitability."""

import csv
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.agents.contract_picker import ContractChoice, ContractPicker
from footprint.agents.greeks import DeltaAgent, GammaAgent, VegaAgent
from footprint.agents.theta import ThetaAgent
from footprint.backtest.__main__ import main
from footprint.backtest.engine import Backtest
from footprint.backtest.model_check import ModelOutsideBand, model_problem
from footprint.backtest.options_model import OptionModel
from footprint.backtest.settings import BacktestSettings
from footprint.core.calendar import SessionKind, session_at
from footprint.core.config import Profiles
from footprint.core.models import Bar, Direction
from footprint.decision.sizing import EntryPlan
from tests.backtest.conftest import TRADE_FROM, extend, workdir
from tests.conftest import REPO_ROOT

D = Decimal
TUE_PRIME_OPEN = datetime(2026, 9, 15, 11, 1, tzinfo=UTC)  # Tuesday 04:01 PT
TUE_LATE = datetime(2026, 9, 15, 20, 50, tzinfo=UTC)  # Tuesday 13:50 PT
FRI_LATE = datetime(2026, 9, 18, 20, 50, tzinfo=UTC)  # Friday 13:50 PT: Monday's expiry
SWEEP = [2000, 3000, 4000, 5000, 6000, 7000, 7500, 8000, 9000, 10000, 15000, 20000]
SWEEP += [23712, 30000, 40000, 60000, 80000, 100000, 150000]


def model(profiles: Profiles, settings: BacktestSettings) -> OptionModel:
    return OptionModel(
        settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
    )


def first_veto(profiles: Profiles, plan: EntryPlan, price: Decimal, t: datetime) -> str | None:
    """The first of Theta, Vega, Delta and Gamma to veto `plan`, as the pipeline runs them."""
    s, inst = profiles.strategy, profiles.instrument
    theta = ThetaAgent(s.theta, inst, profiles.risk).assess(plan, price, t)
    if not theta.allowed:
        return "theta"
    for agent in (
        VegaAgent(s.vega, s.theta.rate, inst),
        DeltaAgent(s.delta, s.vega, s.theta.rate, inst),
        GammaAgent(s.gamma, s.vega, s.theta.rate, inst),
    ):
        if not agent.assess(plan, price, t).allowed:
            return agent.agent_id
    return None


def plan_for(choice: ContractChoice) -> EntryPlan:
    """One contract at the ask: the vetoes judge the contract, not Ezra's sizing (a late
    Friday entry is refused at sizing in the backtest, a separate pre-existing item)."""
    q = choice.quote
    return EntryPlan(choice, 1, q.bid, q.ask, q.ask)


# --- the reviewer's price sweep: the guard says exactly what the vetoes would do --------


@pytest.mark.parametrize("t", [TUE_PRIME_OPEN, TUE_LATE, FRI_LATE])
@pytest.mark.parametrize("price", SWEEP)
def test_the_guard_refuses_exactly_where_the_vetoes_refuse_the_modelled_contract(
    profiles: Profiles, settings: BacktestSettings, price: int, t: datetime
) -> None:
    m = model(profiles, settings)
    at = D(price) + D("7.25")  # off a strike, as a real price is
    problem = model_problem(m, profiles, t, at)
    for direction in Direction:
        chain = m.chain(t, at)
        choice = ContractPicker(profiles.execution, profiles.instrument).choose(chain, direction, t)
        assert isinstance(choice, ContractChoice), choice
        veto = first_veto(profiles, plan_for(choice), at, t)
        if problem is None:
            assert veto is None, (price, direction, veto)
        else:
            assert veto in ("theta", "vega"), (price, direction, veto, problem)
            assert f"{veto}." in problem, problem
            assert f"{veto} would veto" in problem, problem


@pytest.mark.parametrize(
    ("low", "high", "step"),
    [(D(7200), D(7400), D("0.25")), (D(91250), D(92100), D(1))],
)
def test_at_the_edges_of_the_band_the_guard_agrees_with_the_vetoes_price_by_price(
    profiles: Profiles, settings: BacktestSettings, low: Decimal, high: Decimal, step: Decimal
) -> None:
    # Near each edge the verdict flips back and forth with the strike and the option
    # tick: a put can fail where the call passes, and a tie in strike distance decides
    # which contract is judged. The guard must refuse exactly where Felix's long or
    # short pick would be vetoed, and nowhere else.
    m = model(profiles, settings)
    picker = ContractPicker(profiles.execution, profiles.instrument)
    refused = allowed = 0
    at = low
    while at <= high:
        problem = model_problem(m, profiles, TUE_LATE, at)
        vetoes = []
        for direction in Direction:
            choice = picker.choose(m.chain(TUE_LATE, at), direction, TUE_LATE)
            assert isinstance(choice, ContractChoice), choice
            veto = first_veto(profiles, plan_for(choice), at, TUE_LATE)
            if veto is not None:
                vetoes.append((choice.quote.right.value.lower(), choice.quote.strike, veto))
        if problem is None:
            assert not vetoes, (at, vetoes)
            allowed += 1
        else:
            assert vetoes, (at, problem)
            assert any(
                f"{r} {k} " in problem and f"{v} would veto" in problem for r, k, v in vetoes
            ), (at, problem, vetoes)
            refused += 1
        at += step
    assert refused  # the range really straddles an edge
    assert allowed


@pytest.mark.parametrize(
    ("price", "agent", "side"),
    [
        (2000, "theta", "above"),
        (4000, "theta", "above"),
        (5000, "vega", "above"),
        (7000, "vega", "above"),
        (100000, "vega", "below"),
        (150000, "theta", "below"),
    ],
)
def test_the_known_bad_prices_with_todays_draft_numbers(
    profiles: Profiles, settings: BacktestSettings, price: int, agent: str, side: str
) -> None:
    # The facts behind GV-3 and GV-4 (review H1), with the draft config of 2026-10-23.
    problem = model_problem(model(profiles, settings), profiles, TUE_LATE, D(price))
    assert problem is not None
    bound = "min_implied_vol" if side == "below" else "max_implied_vol"
    assert f" {side} {agent}.{bound} " in problem or f", {side} {agent}.{bound} " in problem
    assert f"{agent} would veto every entry" in problem
    assert str(price) in problem


@pytest.mark.parametrize("price", [8000, 20000, 40000])
def test_ordinary_prices_pass(profiles: Profiles, settings: BacktestSettings, price: int) -> None:
    for t in (TUE_PRIME_OPEN, TUE_LATE, FRI_LATE):
        assert model_problem(model(profiles, settings), profiles, t, D(price)) is None


def test_a_problem_names_the_contract_its_volatility_and_the_bound(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    s = profiles.strategy
    problem = model_problem(model(profiles, settings), profiles, TUE_LATE, D(6000))
    assert problem is not None
    assert f"vega.max_implied_vol {s.vega.max_implied_vol}" in problem
    assert "implied volatility 1.2" in problem  # solved inside theta's bounds: about 1.22
    assert "2026-09-16" in problem  # the expiry Felix would take
    problem = model_problem(model(profiles, settings), profiles, TUE_LATE, D(2000))
    assert problem is not None
    assert f"theta.max_implied_vol {s.theta.max_implied_vol}" in problem


def test_the_expiry_checked_is_the_nearest_from_dte_min_even_past_dte_max(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    # Late Friday the next listed expiry is Monday (3 days). With dte_max 1 Felix would
    # pick nothing there, but the model still prices that expiry: a bad price must still
    # be found, never skipped.
    problem = model_problem(model(profiles, settings), profiles, FRI_LATE, D(6000))
    assert problem is not None
    assert "2026-09-21" in problem
    narrow = replace(profiles, instrument=replace(profiles.instrument, dte_max=1))
    problem = model_problem(model(narrow, settings), narrow, FRI_LATE, D(6000))
    assert problem is not None, "no expiry inside dte_max: the nearest one is still checked"
    assert "2026-09-21" in problem


# --- the run refuses to start ---------------------------------------------------------


def shifted(bars: list[Bar] | tuple[Bar, ...], by: Decimal) -> list[Bar]:
    return [
        replace(b, open=b.open + by, high=b.high + by, low=b.low + by, close=b.close + by)
        for b in bars
    ]


def test_a_backtest_over_prices_the_model_cannot_trade_is_refused_before_it_runs(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = extend(history, [D(-10)] * 5)
    bars = shifted(bars, D(6000) - bars[-1].close)  # about 6,000
    lowest = min(b.low for b in bars if b.ts >= TRADE_FROM)
    with pytest.raises(ModelOutsideBand) as refused:
        Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM)
    text = str(refused.value)
    assert str(lowest) in text  # the price at which it fails
    assert "lowest" in text
    assert "vega.max_implied_vol" in text
    assert "GV-4" in text
    assert isinstance(refused.value, ValueError)


def test_a_backtest_over_prices_far_above_the_model_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = extend(history, [D(10)] * 5)
    bars = shifted(bars, D(100000) - bars[-1].close)  # about 100,000
    lowest = min(b.low for b in bars if b.ts >= TRADE_FROM)
    with pytest.raises(ModelOutsideBand) as refused:
        Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM)
    text = str(refused.value)
    assert str(lowest) in text  # every price is too high here; the lowest is named first
    assert "vega.min_implied_vol" in text


def test_the_worst_traded_bar_is_found_not_the_first(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    # Starts near 20,000 (fine), then one later prime bar trades far below.
    bars = extend(history, [D(-10)] * 5)
    late = bars[-1]
    crash = D(6000)
    bars.append(replace(late, ts=late.ts + timedelta(minutes=1), open=late.close, low=crash))
    with pytest.raises(ModelOutsideBand, match=f"{crash}, the lowest"):
        Backtest(profiles, settings, bars, workdir(tmp_path / "low"), trade_from=TRADE_FROM)
    spike = D(100000)
    bars[-1] = replace(late, ts=late.ts + timedelta(minutes=1), open=late.close, high=spike)
    with pytest.raises(ModelOutsideBand, match=f"{spike}, the highest") as refused:
        Backtest(profiles, settings, bars, workdir(tmp_path / "high"), trade_from=TRADE_FROM)
    assert "vega.min_implied_vol" in str(refused.value)


def test_history_before_trade_from_is_not_checked(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    # Look-back bars are never traded: a low price there is not the model's problem.
    bars = list(history)
    old = next(
        i for i, b in enumerate(bars) if session_at(b.ts, profiles.instrument).name == "PRIME"
    )
    bars[old] = replace(bars[old], low=D(6000))
    Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM)


def test_only_prime_session_bars_are_checked(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    # No entry is made outside prime, so a price there cannot leave the run tradeless.
    bars = extend(history, [D(-10)] * 5)
    last = bars[-1]
    evening = datetime(2026, 9, 15, 23, 0, tzinfo=UTC)  # 16:00 PT, overnight session
    assert session_at(evening, profiles.instrument) is SessionKind.OVERNIGHT
    bars.append(replace(last, ts=evening, open=last.close, low=D(6000)))
    Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM)


def test_the_command_line_refuses_with_a_clear_error_and_exit_2(
    history: tuple[Bar, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bars.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        bars = extend(history, [D(-10)] * 5)
        for b in shifted(bars, D(6000) - bars[-1].close):
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    out = tmp_path / "run"
    argv = ["--bars", str(path), "--out", str(out), "--config", str(REPO_ROOT / "config")]
    code = main([*argv, "--trade-from", TRADE_FROM.isoformat()])
    assert code == 2  # INVALID, never PASS or FAIL
    printed = capsys.readouterr()
    assert "refused" in printed.err
    assert "vega.max_implied_vol" in printed.err
    assert not (out / "readout.json").exists()
