"""The real backtest on history (backtest-history-v1): price-level rescaling at the
engine level (BH-2), the guard at the reference level, fail-closed edges of the
calendar, and a run dated in 2010 at 2010's price level.

Synthetic bars here prove the mechanics only: nothing about any rule's profitability.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.__main__ import main
from footprint.backtest.engine import Backtest, BacktestRun
from footprint.backtest.model_check import ModelOutsideBand, check_model_vol
from footprint.backtest.options_model import OptionModel
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar
from footprint.core.options import OptionChain
from tests.backtest.conftest import TRADE_FROM, config_copy, extend, workdir, write_csv
from tests.decision.scenario import LAST_DAY, market

D = Decimal
REF = D(20000)
GOLDEN = Path(__file__).parent / "golden" / "seam_default"
WEEK = timedelta(weeks=1)


def scaled(settings: BacktestSettings, reference: Decimal | None) -> BacktestSettings:
    return replace(settings, option_model=replace(settings.option_model, reference_price=reference))


def times(bars: list[Bar], k: Decimal) -> list[Bar]:
    """Every price multiplied by k: the same percentage path at another level."""
    return [
        replace(b, open=b.open * k, high=b.high * k, low=b.low * k, close=b.close * k) for b in bars
    ]


def moved(bars: list[Bar], weeks: int) -> list[Bar]:
    return [replace(b, ts=b.ts - weeks * WEEK) for b in bars]


def run(
    profiles: Profiles,
    settings: BacktestSettings,
    bars: list[Bar],
    tmp: Path,
    trade_from: datetime = TRADE_FROM,
) -> tuple[Backtest, BacktestRun]:
    bt = Backtest(profiles, settings, bars, workdir(tmp), trade_from=trade_from)
    return bt, bt.run()


def dollars(result: BacktestRun) -> list[tuple[object, ...]]:
    keys = ("exit_reason", "entry_price", "exit_price", "net_pnl", "contract_id")
    return [tuple(t[k] for k in keys) for t in result.trades]


def stopped(history: tuple[Bar, ...]) -> list[Bar]:
    return extend(history, [D(-10)] * 15)  # the golden run's bars: one trade, stopped out


def ridden(history: tuple[Bar, ...]) -> list[Bar]:
    return extend(history, [D(0)] + [D(10)] * 16 + [D(-10)] * 12)  # up, trailed, back


# --- equal to the un-rescaled run where the price already is the reference ------------


@pytest.mark.parametrize("path", [stopped, ridden])
def test_a_trade_anchored_at_the_reference_has_the_unscaled_dollars(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    path: object,
) -> None:
    bars = path(history)  # type: ignore[operator]
    anchor = history[-1].close  # the entry is decided at the open of the next bar
    _, plain = run(profiles, scaled(settings, None), bars, tmp_path / "plain")
    _, at_ref = run(profiles, scaled(settings, anchor), bars, tmp_path / "ref")
    assert plain.trades
    assert dollars(at_ref) == dollars(plain)
    assert at_ref.final_equity == plain.final_equity
    assert at_ref.marked_max_drawdown == plain.marked_max_drawdown
    assert at_ref.worst_open_loss == plain.worst_open_loss


def test_the_golden_trade_and_ledger_are_byte_identical_at_its_own_level(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The golden run (reference off) and the same run with the reference set to the
    price its one entry was decided at: the trade record and the ledger are identical
    byte for byte. (Decisions after the trade read a slightly different N: there the
    price has moved off the reference.)"""
    bars = tmp_path / "bars.csv"
    write_csv(bars, stopped(history))
    out = tmp_path / "run"
    config = config_copy(tmp_path / "config", history[-1].close)
    with contextlib.redirect_stdout(io.StringIO()):
        main(
            [
                "--bars",
                str(bars),
                "--out",
                str(out),
                "--config",
                str(config),
                "--trade-from",
                TRADE_FROM.isoformat(),
            ]
        )
    for name in ("journal/trades.jsonl", "ledger.json"):
        got = (out / name).read_bytes()
        got = got.replace(str(tmp_path).encode(), b"<TMP>")
        want = (GOLDEN / name).read_bytes()
        assert _no_suffix(got) == _no_suffix(want), name


def _no_suffix(data: bytes) -> bytes:
    return re.sub(rb'("position_id": "[0-9T]+-\d+-)[0-9a-f]{8}"', rb'\1<RAND>"', data)


# --- a trade's dollars depend only on its percentage path ---------------------------


@pytest.mark.parametrize("path", [stopped, ridden])
@pytest.mark.parametrize("k", [D(2), D("0.5"), D("0.075")])
def test_the_same_percentage_path_at_another_level_has_the_same_dollars(
    profiles: Profiles,
    settings: BacktestSettings,
    history: tuple[Bar, ...],
    tmp_path: Path,
    path: object,
    k: Decimal,
) -> None:
    bars = path(history)  # type: ignore[operator]
    s = scaled(settings, REF)
    _, here = run(profiles, s, bars, tmp_path / "here")
    _, there = run(profiles, s, times(bars, k), tmp_path / "there")
    assert here.trades
    assert dollars(there) == dollars(here)
    assert there.final_equity == here.final_equity


def test_the_levels_room_is_judged_at_the_datas_level(
    profiles: Profiles, settings: BacktestSettings, tmp_path: Path
) -> None:
    """BH-4: a prior-day high 250 points above the entry at about 23,700 is 1.9 N away
    at the data's level (N = 111.11 x 23,716 / 20,000), room enough to fire; at 0.075x
    the level it is about 19 points away and N is about 9.9: the same room, the same
    trade. Judged with an unconverted N (111 points) it would be a wall there."""
    walled = extend(tuple(market(wall_above=D(250))), [D(-10)] * 15)
    s = scaled(settings, REF)
    _, here = run(profiles, s, walled, tmp_path / "here")
    _, there = run(profiles, s, times(walled, D("0.075")), tmp_path / "there")
    assert len(here.trades) == 1
    assert dollars(there) == dollars(here)


def test_without_rescaling_the_level_changes_the_dollars(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The control: the same percentage path at twice the level, unscaled, is another
    trade (the model's premium does not grow with the price)."""
    bars = ridden(history)
    s = scaled(settings, None)
    _, here = run(profiles, s, bars, tmp_path / "here")
    _, there = run(profiles, s, times(bars, D(2)), tmp_path / "there")
    assert dollars(there) != dollars(here)


# --- what the model is quoted at -------------------------------------------------------


class Spy(Backtest):
    chains: list[OptionChain]

    def _chain(self, now: datetime) -> OptionChain:
        chain = super()._chain(now)
        self.chains.append(chain)
        return chain


def test_the_decision_sees_a_chain_at_the_reference_level(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = times(stopped(history), D("0.075"))  # about 1,780: 2010's level
    Spy.chains = []
    bt = Spy(profiles, scaled(settings, REF), bars, workdir(tmp_path), trade_from=TRADE_FROM)
    result = bt.run()
    assert Spy.chains
    first = Spy.chains[0]
    assert first.underlying_price == REF  # idle: the anchor is the price itself
    strikes = sorted({q.strike for q in first.quotes})
    assert strikes[0] < REF < strikes[-1]
    (trade,) = result.trades
    assert str(trade["contract_id"]).startswith(f"CALL{REF}-")


def test_a_chain_asked_for_between_publishes_is_rescaled_too(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Mutation survivor 52: the runner can decide in a step whose full chain was not
    published (a working entry ends inside that step, then it decides); the chain it
    then gets is built on the spot, and must be at the model's level as well."""
    bars = times(stopped(history), D("0.075"))
    bt = Backtest(profiles, scaled(settings, REF), bars, workdir(tmp_path), trade_from=TRADE_FROM)
    chain = bt._chain(TRADE_FROM)  # nothing published yet
    assert chain.underlying_price == REF  # the anchor is the first bar's open, the price too
    bt._level.anchor(bars[0].open * 2)
    assert bt._chain(TRADE_FROM).underlying_price == REF / 2


def test_a_live_trade_is_quoted_at_its_frozen_scale(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = times(stopped(history), D("0.075"))
    seen: list[tuple[Decimal, Decimal]] = []

    class Probe(Backtest):
        def _publish(self, t: datetime, *, full_chain: bool) -> None:
            super()._publish(t, full_chain=full_chain)
            if self.runner.position is not None:
                seen.append((self._price, self._level.model_price(self._price)))

    anchor = bars[len(history) - 1].close
    Probe(profiles, scaled(settings, REF), bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert seen
    for price, model in seen:
        assert model == price * REF / anchor


# --- the guard at the reference level ------------------------------------------------


def test_with_rescaling_a_2010_price_level_passes_the_guard(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = times(stopped(history), D("0.075"))
    with pytest.raises(ModelOutsideBand):
        Backtest(
            profiles, scaled(settings, None), bars, workdir(tmp_path / "a"), trade_from=TRADE_FROM
        )
    _, result = run(profiles, scaled(settings, REF), bars, tmp_path / "b")
    assert len(result.trades) == 1


def test_the_guard_checks_the_reference_itself(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...]
) -> None:
    bars = times(stopped(history), D("0.075"))
    traded = [b for b in bars if b.ts >= TRADE_FROM]
    model = OptionModel(
        settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
    )
    check_model_vol(model, profiles, traded, reference=REF)  # passes
    with pytest.raises(ModelOutsideBand, match="6000"):
        check_model_vol(model, profiles, traded, reference=D(6000))
    with pytest.raises(ModelOutsideBand, match="reference_price"):
        check_model_vol(model, profiles, traded, reference=D(6000))
    with pytest.raises(ModelOutsideBand):
        check_model_vol(model, profiles, traded)  # unscaled: 1,780 is outside the band


def test_a_reference_the_model_cannot_trade_is_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    with pytest.raises(ModelOutsideBand, match="reference_price"):
        Backtest(
            profiles,
            scaled(settings, D(6000)),
            stopped(history),
            workdir(tmp_path),
            trade_from=TRADE_FROM,
        )


# --- fail closed at the calendar's edges ---------------------------------------------


def test_look_back_bars_before_the_calendar_are_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Outside calendar_from no holiday is known, so a reader could take a holiday
    stub there for a session (its prior-day levels, its 20-session averages): refused,
    look-back or not."""
    start = profiles.instrument.calendar_from
    early = replace(history[0], ts=datetime(start.year - 1, 6, 1, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match="before calendar_from"):
        Backtest(profiles, settings, [early, *history], workdir(tmp_path), trade_from=TRADE_FROM)


def test_a_look_back_bar_on_the_first_calendar_day_is_accepted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    start = profiles.instrument.calendar_from  # 04:00 PT on it: its own local date
    first = replace(history[0], ts=datetime(start.year, start.month, start.day, 11, tzinfo=UTC))
    Backtest(profiles, settings, [first, *history], workdir(tmp_path), trade_from=TRADE_FROM)
    before = replace(first, ts=first.ts - timedelta(hours=12))  # the evening before, PT
    with pytest.raises(ValueError, match="before calendar_from"):
        Backtest(
            profiles, settings, [before, *history], workdir(tmp_path / "b"), trade_from=TRADE_FROM
        )


def test_no_expiry_is_listed_past_the_calendar(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    inst = profiles.instrument
    model = OptionModel(settings.option_model, inst, profiles.strategy.options.assumed_delta)
    through = inst.calendar_through
    near_end = datetime(through.year, through.month, through.day, 14, tzinfo=UTC) - timedelta(
        days=1
    )
    expiries = {q.expiry for q in model.chain(near_end, REF).quotes}
    assert expiries == {through}
    early = datetime(2026, 9, 15, 14, tzinfo=UTC)
    assert len({q.expiry for q in model.chain(early, REF).quotes}) == (
        settings.option_model.expiries_listed
    )


# --- dated in 2010, at 2010's level ------------------------------------------------------


def _weeks_to_2010() -> int:
    target = date(2010, 8, 17)  # a Tuesday, as LAST_DAY is
    days = (LAST_DAY - target).days
    assert days % 7 == 0
    return days // 7


def test_a_2010_run_at_2010_prices_trades_end_to_end(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    weeks = _weeks_to_2010()
    bars = moved(times(stopped(history), D("0.075")), weeks)
    _, result = run(profiles, scaled(settings, REF), bars, tmp_path, TRADE_FROM - weeks * WEEK)
    assert result.ended_early is None
    (trade,) = result.trades
    assert trade["exit_reason"] == "STOP"
    assert str(trade["contract_id"]).startswith(f"CALL{REF}-2010")


def test_the_command_line_runs_a_2010_csv(
    history: tuple[Bar, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    weeks = _weeks_to_2010()
    path = tmp_path / "bars.csv"
    write_csv(path, moved(times(stopped(history), D("0.075")), weeks))
    out = tmp_path / "run"
    config = config_copy(tmp_path / "config", REF)
    argv = ["--bars", str(path), "--out", str(out), "--config", str(config)]
    code = main([*argv, "--trade-from", (TRADE_FROM - weeks * WEEK).isoformat()])
    assert code == 1  # NOT ENOUGH TRADES: it ran
    readout = json.loads((out / "readout.json").read_text())
    assert readout["whole"]["trades"] == 1
    assumptions = readout["assumptions"]
    assert assumptions["option model reference_price"] == "20000"
    assert "rescaled" in assumptions["price level"]
    printed = capsys.readouterr().out
    assert "price level" in printed


def test_the_readout_says_when_nothing_is_rescaled(
    history: tuple[Bar, ...], tmp_path: Path
) -> None:
    path = tmp_path / "bars.csv"
    write_csv(path, stopped(history))
    out = tmp_path / "run"
    with contextlib.redirect_stdout(io.StringIO()):
        config = config_copy(tmp_path / "config", None)
        argv = ["--bars", str(path), "--out", str(out), "--config", str(config)]
        main([*argv, "--trade-from", TRADE_FROM.isoformat()])
    assumptions = json.loads((out / "readout.json").read_text())["assumptions"]
    assert assumptions["option model reference_price"] == "None"
    assert "price level" not in assumptions
