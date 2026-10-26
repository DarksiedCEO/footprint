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
from footprint.core.options import OptionChain
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
    moves = [D(0)] + [D(10)] * 16 + [D(-10)] * 12  # fill, up ~160 points, back down
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
    bars = extend(history, [D(0)] + [D(5)] * 3)
    gap = timedelta(minutes=3)
    later = [
        Bar(b.symbol, b.ts + gap, b.open, b.high, b.low, b.close, b.volume)
        for b in extend(history, [D(0)] + [D(5)] * 7)[len(bars) :]
    ]
    _, result = run(profiles, settings, [*bars, *later], tmp_path)
    assert any("no fresh quote" in a.message for a in result.alarms)  # the feed went quiet
    assert result.ended_early is None


def test_the_ledger_and_the_journal_agree(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    _, result = run(profiles, settings, extend(history, [D(-10)] * 15), tmp_path)
    assert read_out(result, settings, {}).ledger_matches_journal


def test_a_trade_open_when_the_data_ends_is_sold_and_counted(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Review B2: a run that ends inside a trade carries that trade's result."""
    _, result = run(profiles, settings, extend(history, [D(0), D(-20), D(-20)]), tmp_path)
    assert not result.open_at_end
    trade = only_trade(result)
    assert trade["exit_reason"] == "END_OF_DATA"
    assert D(str(trade["net_pnl"])) < 0  # the loss it was carrying
    r = read_out(result, settings, {})
    assert r.ledger_matches_journal
    assert r.whole.trades == 1


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


def test_the_decision_window_is_capped_at_history_minutes(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    from dataclasses import replace

    small = replace(settings, engine=replace(settings.engine, history_minutes=500))
    seen: list[int] = []

    class Spy(Backtest):
        def _closed_bars(self, now: datetime) -> tuple[Bar, ...]:
            window = super()._closed_bars(now)
            seen.append(len(window))
            return window

    Spy(
        profiles, small, extend(history, [D(1)] * 3), workdir(tmp_path), trade_from=TRADE_FROM
    ).run()
    assert seen
    assert max(seen) == 500


def test_inside_a_minute_the_bad_extreme_comes_first_for_an_open_trade(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Long calls, then a falling bar that also spikes far up. Against the trade the
    low comes first: the hard stop, not a trailed profit off the spike."""
    bars = extend(history, [D(0)])  # the entry fills on this bar's dip
    o = bars[-1].close
    bars.append(Bar("NQ", bars[-1].ts + timedelta(minutes=1), o, o + 250, o - 120, o - 50, 100))
    _, result = run(profiles, settings, bars, tmp_path)
    trade = only_trade(result)
    assert D(str(trade["net_pnl"])) < 0
    assert -D(str(trade["net_pnl"])) >= profiles.risk.stop_loss


def test_the_last_bar_is_decided_on_when_it_closes(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The scenario ends with the follow-through bar: its close (07:01) fires."""
    bt = Backtest(profiles, settings, list(history), workdir(tmp_path), trade_from=TRADE_FROM)
    result = bt.run()
    assert result.fired == 1
    assert bt.journal.decisions()[-1]["as_of"] == DECISION_TIME.isoformat()


def test_a_working_entry_sees_the_good_extreme_first(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Review B1: the bar spikes 40 and dips 4. Worst case for a working long entry:
    the high first, the dip only near the end of the minute, after the entry's
    timeout: no fill, no trade (a low-first path would have filled before the spike)."""
    o = history[-1].close
    spike = Bar("NQ", DECISION_TIME, o, o + 40, o - 4, o, 100)
    _, result = run(profiles, settings, [*history, spike], tmp_path)
    assert result.trades == []
    assert not result.open_at_end


def test_the_minute_a_trade_closes_the_next_decision_is_made(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Review B3: live, the runner decides a second after it is idle again."""
    bt, result = run(profiles, settings, extend(history, [D(-10)] * 15), tmp_path)
    trade = only_trade(result)
    exit_ts = datetime.fromisoformat(str(trade["exit_ts"]))
    minute_end = exit_ts.replace(second=0) + timedelta(minutes=1)
    later = [datetime.fromisoformat(d["as_of"]) for d in bt.journal.decisions()]
    assert any(exit_ts < t < minute_end for t in later)


def test_equity_is_also_marked_inside_trades(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    _, result = run(profiles, settings, extend(history, [D(-10)] * 15), tmp_path)
    trade = only_trade(result)
    assert result.worst_open_loss <= D(str(trade["net_pnl"]))  # at least as bad as the exit
    assert result.marked_max_drawdown >= -D(str(trade["net_pnl"]))


def test_an_entry_still_working_when_the_data_ends_is_cancelled(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """The last bar's close fires an entry on the very last step: it is cancelled, not
    left resting at the broker after the run."""
    bt, result = run(profiles, settings, list(history), tmp_path)
    assert result.fired == 1
    assert result.trades == []
    assert bt.broker.open_orders() == []
    assert not result.open_at_end


def test_every_chain_a_decision_sees_is_the_book_at_that_moment(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Review B3-r2 (R12 in the backtest): whenever the runner decides, at the open or
    later in a minute after a close, every quote of its chain is in the book, fresh
    and equal, so Diego can send whatever the decision approves."""
    checked: list[datetime] = []

    class Spy(Backtest):
        def _chain(self, now: datetime) -> OptionChain:
            chain = super()._chain(now)
            for q in chain.quotes:
                live = self.book.latest(q.contract_id)
                assert live is not None
                assert (live.bid, live.ask, live.ts) == (q.bid, q.ask, now)
            checked.append(now)
            return chain

    bt = Spy(
        profiles, settings, extend(history, [D(-10)] * 15), workdir(tmp_path), trade_from=TRADE_FROM
    )
    result = bt.run()
    trade = only_trade(result)
    exit_ts = datetime.fromisoformat(str(trade["exit_ts"]))
    assert any(t.second != 0 for t in checked)  # a decision later in a minute was checked
    assert any(exit_ts < t < exit_ts + timedelta(minutes=1) for t in checked)


class _FakeRunner:
    def __init__(self, position: object, exposure: object) -> None:
        self.position = position
        self.exposure = exposure


def test_a_new_entry_inside_a_bar_gets_its_own_worst_case_order(
    profiles: Profiles, settings: BacktestSettings, history: tuple[Bar, ...], tmp_path: Path
) -> None:
    """Review B3-r2: a long stopped out mid-bar left the order low-first; a new long
    entry in the same bar must see the high first, not inherit the old order."""
    from footprint.core.models import Direction

    bt = Backtest(profiles, settings, list(history), workdir(tmp_path), trade_from=TRADE_FROM)
    bt.runner = _FakeRunner(None, Direction.LONG)  # type: ignore[assignment]  # entry working
    assert bt._next_order(30, None, Direction.LONG) is Direction.SHORT  # high first
    bt.runner = _FakeRunner(object(), Direction.LONG)  # type: ignore[assignment]  # it filled
    assert bt._next_order(35, Direction.LONG, Direction.SHORT) is Direction.SHORT  # kept
