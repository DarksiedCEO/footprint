"""The backtest engine: the real system on a simulated clock."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.engine import Backtest, BacktestRun
from footprint.backtest.readout import read_out
from footprint.backtest.settings import BacktestSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar
from tests.backtest.conftest import TRADE_FROM, extend, workdir
from tests.decision.scenario import DECISION_TIME

D = Decimal
SEC = timedelta(seconds=1)


def run(
    profiles: Profiles, settings: BacktestSettings, bars: list[Bar], tmp_path: Path
) -> tuple[Backtest, BacktestRun]:
    bt = Backtest(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM)
    return bt, bt.run()


def only_trade(result: BacktestRun) -> dict[str, object]:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


def test_a_falling_market_stops_the_trade_out_at_the_resting_stop(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    _, result = run(profiles, settings, extend(history, [D(-10)] * 15), tmp_path)
    trade = only_trade(result)
    assert trade["exit_reason"] == "STOP"
    entry, exit_ = D(str(trade["entry_price"])), D(str(trade["exit_price"]))
    fees = 2 * profiles.risk.fee_per_contract_per_side
    assert D(str(trade["net_pnl"])) == (exit_ - entry) * profiles.instrument.point_value - fees
    # The stop sits $stop_loss under the entry; its limit is the worst it may fill.
    loss = -D(str(trade["net_pnl"]))
    assert profiles.risk.stop_loss <= loss <= profiles.risk.stop_max
    assert result.final_equity == result.bankroll + D(str(trade["net_pnl"]))
    assert result.ended_early is None
    assert not result.open_at_end


def test_a_rising_market_is_ridden_and_the_trail_locks_profit(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    moves = [D(10)] * 16 + [D(-10)] * 12  # up ~160 points, then back down
    _, result = run(profiles, settings, extend(history, moves), tmp_path)
    trade = only_trade(result)
    assert trade["exit_reason"] == "STOP"  # the trailed resting stop
    assert D(str(trade["net_pnl"])) > 0  # the trail locked part of the peak


def test_decisions_never_see_a_bar_before_it_closes(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    seen: list[tuple[datetime, tuple[Bar, ...]]] = []

    class Spy(Backtest):
        def _closed_bars(self, now: datetime) -> tuple[Bar, ...]:
            window = super()._closed_bars(now)
            seen.append((now, window))
            return window

    bars = extend(history, [D(-10)] * 5)
    Spy(profiles, settings, bars, workdir(tmp_path), trade_from=TRADE_FROM).run()
    assert seen
    for now, window in seen:
        assert all(bar.ts + timedelta(minutes=1) <= now for bar in window)
        assert len(window) <= settings.engine.history_minutes


def test_with_nothing_live_outside_prime_the_runner_is_not_stepped(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """From the overnight session before the last day: those minutes are counted, and
    no decision is made in them."""
    start = history[-1].ts.replace(hour=0, minute=0)
    bt = Backtest(profiles, settings, list(history), workdir(tmp_path), trade_from=start)
    result = bt.run()
    assert result.skipped_minutes > 0
    assert all(
        datetime.fromisoformat(d["as_of"]) >= TRADE_FROM - timedelta(hours=3)
        for d in bt.journal.decisions()
    )


def test_a_gap_in_the_data_is_stepped_through_with_no_quotes(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bars = extend(history, [D(5)] * 4)
    gap = timedelta(minutes=3)
    later = [
        Bar(b.symbol, b.ts + gap, b.open, b.high, b.low, b.close, b.volume)
        for b in extend(history, [D(5)] * 8)[len(bars) :]
    ]
    _, result = run(profiles, settings, [*bars, *later], tmp_path)
    assert any("no fresh quote" in a.message for a in result.alarms)  # the feed went quiet
    assert result.ended_early is None


def test_the_ledger_and_the_journal_agree(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    _, result = run(profiles, settings, extend(history, [D(-10)] * 15), tmp_path)
    assert read_out(result, settings, {}).ledger_matches_journal


def test_a_trade_open_when_the_data_ends_is_reported_not_counted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    _, result = run(profiles, settings, extend(history, [D(2)] * 3), tmp_path)
    assert result.open_at_end
    assert result.trades == []
    assert any("still open" in why for why in read_out(result, settings, {}).why)


def test_a_halt_ends_the_run_early(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    bt = Backtest(
        profiles, settings, extend(history, [D(1)] * 5), workdir(tmp_path), trade_from=TRADE_FROM
    )
    bt.halt.trip("a defect")
    result = bt.run()
    assert result.ended_early is not None
    assert result.ended_early.startswith("halted")
    assert read_out(result, settings, {}).verdict == "INVALID"


def test_every_run_starts_in_an_empty_directory(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    out = workdir(tmp_path)
    out.mkdir()
    (out / "old.txt").write_text("left over")
    with pytest.raises(FileExistsError):
        Backtest(profiles, settings, list(history), out)


def test_bad_inputs_are_refused(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    es = [Bar("ES", b.ts, b.open, b.high, b.low, b.close, b.volume) for b in history[:5]]
    with pytest.raises(ValueError, match="not 'NQ'"):
        Backtest(profiles, settings, es, tmp_path / "a")
    with pytest.raises(ValueError, match="after the last bar"):
        Backtest(
            profiles,
            settings,
            list(history),
            tmp_path / "b",
            trade_from=DECISION_TIME + timedelta(days=1),
        )
