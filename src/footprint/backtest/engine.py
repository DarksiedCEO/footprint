"""The backtest engine: the real system, unchanged, on a simulated clock.

Every agent, the pipeline, Diego, Booker, Malik, the ledger, the journal and the
runner are the live objects; only two things are swapped, behind the same seams:
the data (historical 1-minute bars instead of a live feed) and the broker (the
simulator, filling only against the shared quote book, R12).

The clock, minute by minute:
- At each bar's open, the bars that have closed are visible to the decision and
  nothing later is (no look-ahead). The modelled option chain at that moment is
  published to the quote book, then the runner steps.
- While an entry or a position is live, the runner steps every `substep_seconds`
  through the minute on the modelled path (adverse-first), the live contract quoted
  at each step. Through a gap in the data it keeps stepping with no new quotes, as
  it would on a dead feed.
- In prime, once nothing is live again (a trade closed and reconcile agreed), it
  keeps stepping inside the minute until the runner has decided on the newest
  closed bar, as it would live a second later.
- With nothing live, outside the prime session, the runner is not stepped: Diego
  sends no entry outside prime, so nothing can happen. Those minutes are counted.
- The path's order is fixed per bar, worst case for what is live after the open:
  an open trade sees its bad extreme first; a working entry sees its good extreme
  first (it can fill only after the best price has passed).
- When the history ends, a trade still open is sold at the bid through the normal
  exit path and booked (END_OF_DATA). Equity is also marked to the bid at every
  step, for the drawdown inside trades.

The run ends early on any halt or a latched kill switch (live, those wait for
Andre), and the readout says why.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from footprint.backtest.options_model import OptionModel
from footprint.backtest.path import path_price
from footprint.backtest.settings import BacktestSettings
from footprint.core.calendar import SessionKind, local_minute, session_at
from footprint.core.config import _PERCENT_MAX, Profiles
from footprint.core.models import Bar, Direction
from footprint.core.options import OptionChain
from footprint.core.series import require_series
from footprint.decision.pipeline import EntryTrigger, Pipeline
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper
from footprint.execution.executor.diego import Executor
from footprint.execution.halt import Halt
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import Alarm, ExitRule, Manager
from footprint.execution.market import QuoteBook
from footprint.execution.orders import SimulatedBroker, TrackedBroker
from footprint.execution.runner import Runner

_MINUTE = timedelta(minutes=1)
_MINUTE_SECONDS = int(_MINUTE.total_seconds())
_PCT = _PERCENT_MAX


@dataclass
class BacktestRun:
    """What happened. Trades come from the system's own journal."""

    first_bar: datetime
    last_bar: datetime
    bankroll: Decimal
    final_equity: Decimal
    trades: list[dict[str, Any]]
    decisions: int
    fired: int
    alarms: list[Alarm] = field(default_factory=list)
    ended_early: str | None = None
    open_at_end: bool = False
    skipped_minutes: int = 0
    steps: int = 0
    marked_max_drawdown_pct: Decimal = Decimal(0)  # equity marked to the bid each step
    marked_max_drawdown: Decimal = Decimal(0)
    worst_open_loss: Decimal = Decimal(0)  # the worst unrealized P&L of any trade


class Backtest:
    def __init__(
        self,
        profiles: Profiles,
        settings: BacktestSettings,
        bars: Sequence[Bar],
        workdir: Path,
        blackouts: Callable[[datetime], Iterable[NewsBlackout]] = lambda now: (),
        trade_from: datetime | None = None,
        trigger: EntryTrigger | None = None,
        exit_rule: ExitRule | None = None,
    ) -> None:
        """`trade_from`: bars before it are history only (the readers' look-back); the
        clock starts at the first bar at or after it. None: start at the first bar.
        `trigger`: a named entry rule's trigger (footprint.backtest.rules); None is
        the live entry, unchanged. `exit_rule`: a named exit rule; None is the live
        exit, unchanged."""
        self._p = profiles
        self._s = settings
        self._bars = tuple(bars)
        require_series(self._bars, 1, "backtest bars")
        if self._bars[0].symbol != profiles.instrument.symbol:
            raise ValueError(
                f"bars are for {self._bars[0].symbol!r}, not {profiles.instrument.symbol!r}"
            )
        workdir.mkdir(parents=True, exist_ok=True)
        if any(workdir.iterdir()):
            raise FileExistsError(f"{workdir} is not empty: every backtest starts fresh")
        self._starts = [bar.ts for bar in self._bars]
        self._first = 0 if trade_from is None else bisect_left(self._starts, trade_from)
        if self._first >= len(self._bars):
            raise ValueError("trade_from is after the last bar")
        self._inside_calendar(self._bars[self._first].ts, self._bars[-1].ts)
        self._model = OptionModel(
            settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
        )
        self._price = self._bars[0].open
        self._published: OptionChain | None = None
        self._window_end = -1
        self._window: tuple[Bar, ...] = ()
        self.alarms: list[Alarm] = []
        self._peak = profiles.risk.bankroll
        self._worst = self._worst_pct = self._worst_open = Decimal(0)

        p = profiles
        self.book = QuoteBook()
        self.broker = SimulatedBroker(self.book, p.execution.quotes.max_age_seconds)
        tracked = TrackedBroker(self.broker)
        self.halt = Halt()
        self.ledger = Ledger(p.risk, p.instrument, StateStore(workdir / "ledger.json"), create=True)
        pipeline = Pipeline(p) if trigger is None else Pipeline(p, trigger)
        stops = StopKeeper(p.risk, p.instrument, p.execution)
        executor = Executor(p, pipeline, tracked, stops, self.ledger, self.halt, self.book)
        manager = (
            Manager(p, tracked, stops, self.ledger, self.halt, self.book)
            if exit_rule is None
            else Manager(p, tracked, stops, self.ledger, self.halt, self.book, exit_rule=exit_rule)
        )
        self.journal = Journal(workdir / "journal")
        self.runner = Runner(
            pipeline,
            executor,
            manager,
            self.ledger,
            self.journal,
            self.halt,
            bars=self._closed_bars,
            chain=self._chain,
            blackouts=blackouts,
            on_alarm=self.alarms.append,
            started=self._bars[self._first].ts,
            book=self.book,
            settings=p.execution.runner,
        )
        self.runner.clear_to_trade("backtest")

    # --- the seams the runner reads -------------------------------------------------

    def _closed_bars(self, now: datetime) -> tuple[Bar, ...]:
        """Bars that had closed by `now` (no look-ahead), at most history_minutes."""
        end = bisect_right(self._starts, now - _MINUTE)
        if end != self._window_end:
            start = max(0, end - self._s.engine.history_minutes)
            self._window, self._window_end = self._bars[start:end], end
        return self._window

    def _chain(self, now: datetime) -> OptionChain:
        if self._published is not None and self._published.ts == now:
            return self._published
        return self._model.chain(now, self._price)

    # --- the clock ------------------------------------------------------------------

    def run(self) -> BacktestRun:
        substep = self._s.engine.substep_seconds
        skipped = steps = 0
        ended: str | None = None
        bars = self._bars
        for i in range(self._first, len(bars)):
            bar = bars[i]
            newest_closed = bars[i - 1].ts if i > 0 else None
            against = None  # the path's order, fixed for the whole bar after the open
            second = 0
            while second < _MINUTE_SECONDS:
                t = bar.ts + timedelta(seconds=second)
                if second == 0 and self._idle() and not self._prime(t):
                    skipped += 1
                    break
                self._price = path_price(bar, second, against)
                # Whenever the runner may decide (idle), the whole chain it will see is
                # in the book at that moment, so Diego can send what it approves (R12).
                self._publish(t, full_chain=second == 0 or self._idle())
                self._mark(t)  # the new quote, before anything fills against it
                before = self.runner.exposure
                self.runner.step(t)
                steps += 1
                self._mark(t)
                ended = self._stopped(t)
                if ended is not None:
                    break
                against = self._next_order(second, before, against)
                if self._idle() and self._decided(newest_closed, t):
                    break
                second += substep
            if ended is not None:
                break
            if i + 1 < len(bars):
                ended = self._through_gap(bar.ts + _MINUTE, bars[i + 1].ts)
                if ended is not None:
                    break
        open_at_end = False
        if ended is None:
            # The last bar closes too: one more step sees it (and decides on it). Then
            # anything still open is sold at the bid and booked (END_OF_DATA), so a run
            # that ends inside a losing trade carries that loss.
            t = bars[-1].ts + _MINUTE
            self._price = bars[-1].close
            self._publish(t, full_chain=True)
            self.runner.step(t)
            steps += 1
            ended = self._stopped(t)
            if ended is None and self.runner.exposure is not None:
                self._publish(t, full_chain=True)
                open_at_end = not self.runner.close_out(t)
            self._mark(t)
        trades = self.journal.trades()
        decisions = self.journal.decisions()
        return BacktestRun(
            first_bar=bars[self._first].ts,
            last_bar=bars[-1].ts,
            bankroll=self._p.risk.bankroll,
            final_equity=self.ledger.account(bars[-1].ts + _MINUTE).equity,
            trades=trades,
            decisions=len(decisions),
            fired=sum(1 for d in decisions if d["fired"]),
            alarms=self.alarms,
            ended_early=ended,
            open_at_end=open_at_end,
            skipped_minutes=skipped,
            steps=steps,
            marked_max_drawdown_pct=self._worst_pct,
            marked_max_drawdown=self._worst,
            worst_open_loss=self._worst_open,
        )

    def _inside_calendar(self, first: datetime, last: datetime) -> None:
        """The traded window must lie inside the maintained exchange calendar. Outside
        it every day is closed (fail closed: no holiday or early close is known there),
        so a backtest over it would run without an error and trade nothing (delta review
        2, N-2). Look-back bars before `trade_from` may lie anywhere: they are never
        traded."""
        instrument = self._p.instrument
        start = local_minute(first, instrument.prime_session)[0]
        end = local_minute(last, instrument.prime_session)[0]
        if start < instrument.calendar_from:
            raise ValueError(
                f"the backtest trades from {start}, before calendar_from "
                f"{instrument.calendar_from}: every day there is closed. Add that year's "
                "holidays, early closes, no-trade days and Easter from the exchange's calendar and "
                "move calendar_from back first"
            )
        if end > instrument.calendar_through:
            raise ValueError(
                f"the backtest trades until {end}, past calendar_through "
                f"{instrument.calendar_through}: every day there is closed. Extend the "
                "calendar from the exchange's schedule first"
            )

    def _idle(self) -> bool:
        return self.runner.exposure is None and not self.runner.reconciling

    def _prime(self, t: datetime) -> bool:
        return session_at(t, self._p.instrument) is SessionKind.PRIME

    def _decided(self, newest_closed: datetime | None, t: datetime) -> bool:
        """Live, the runner steps every second: once idle again (say, a trade closed
        and reconcile agreed), it decides on the newest closed bar within the same
        minute. Keep stepping until it has, in prime (outside it nothing can enter)."""
        if newest_closed is None or not self._prime(t):
            return True
        last = self.runner.last_decided
        return last is not None and last >= newest_closed

    def _next_order(
        self, second: int, before: Direction | None, against: Direction | None
    ) -> Direction | None:
        """The path's order is chosen after the open, and chosen again when something
        new goes live inside the bar (a fresh entry after a close): worst case for it.
        An entry filling into a position keeps the order it had."""
        if second == 0 or (before is None and self.runner.exposure is not None):
            return self._against()
        return against

    def _against(self) -> Direction | None:
        """The path's order for the rest of this bar, worst case for what is live:
        an open long sees the low first; a working long entry sees the high first
        (it can fill only on the way down, after the bar's best price has passed).
        Shorts mirrored; nothing live: by the bar's own direction."""
        position = self.runner.position
        if position is not None:
            return position.direction
        working = self.runner.exposure
        if working is None:
            return None
        return Direction.SHORT if working is Direction.LONG else Direction.LONG

    def _mark(self, t: datetime) -> None:
        """Equity marked to the bid (less the round-trip fees), for the drawdown the
        realized curve cannot show, and each trade's worst open loss."""
        equity = self.ledger.account(t).equity
        position = self.runner.position
        if position is not None:
            quote = self.book.latest(position.contract_id)
            if quote is not None:
                p = self._p
                fees = 2 * p.risk.fee_per_contract_per_side * position.contracts
                open_pnl = (quote.bid - position.entry_price) * p.instrument.point_value
                open_pnl = open_pnl * position.contracts - fees
                equity += open_pnl
                self._worst_open = min(self._worst_open, open_pnl)
        self._peak = max(self._peak, equity)
        self._worst = max(self._worst, self._peak - equity)
        if self._peak > 0:
            self._worst_pct = max(self._worst_pct, (self._peak - equity) / self._peak * _PCT)

    def _through_gap(self, start: datetime, next_bar: datetime) -> str | None:
        """Missing minutes with a trade live: keep stepping, with no new quotes."""
        t = start
        while t < next_bar and self.runner.exposure is not None:
            self.runner.step(t)
            self._mark(t)
            stopped = self._stopped(t)
            if stopped is not None:
                return stopped
            t += timedelta(seconds=self._s.engine.substep_seconds)
        return None

    def _publish(self, t: datetime, *, full_chain: bool) -> None:
        if full_chain:
            self._published = self._model.chain(t, self._price)
            for quote in self._model.broker_quotes(self._published):
                self.book.publish(quote)
            return
        for contract_id in self._live_contracts():
            self.book.publish(self._model.quote(contract_id, t, self._price))

    def _live_contracts(self) -> set[str]:
        position = self.runner.position
        ids = {position.contract_id} if position is not None else set()
        ids |= {order.contract_id for order in self.broker.open_orders()}
        return ids

    def _stopped(self, t: datetime) -> str | None:
        if self.halt.tripped:
            return "halted: " + "; ".join(self.halt.reasons)
        if self.ledger.account(t).kill_switch_latched:
            return "kill switch latched (live, only Andre can clear it)"
        return None
