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
- With nothing live, outside the prime session, the runner is not stepped: Diego
  sends no entry outside prime, so nothing can happen. Those minutes are counted.

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
from footprint.core.calendar import SessionKind, session_at
from footprint.core.config import Profiles
from footprint.core.models import Bar
from footprint.core.options import OptionChain
from footprint.core.series import require_series
from footprint.decision.pipeline import Pipeline
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper
from footprint.execution.executor.diego import Executor
from footprint.execution.halt import Halt
from footprint.execution.journal.josephine import Journal
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import Alarm, Manager
from footprint.execution.market import QuoteBook
from footprint.execution.orders import SimulatedBroker, TrackedBroker
from footprint.execution.runner import Runner

_MINUTE = timedelta(minutes=1)
_MINUTE_SECONDS = int(_MINUTE.total_seconds())


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


class Backtest:
    def __init__(
        self,
        profiles: Profiles,
        settings: BacktestSettings,
        bars: Sequence[Bar],
        workdir: Path,
        blackouts: Callable[[datetime], Iterable[NewsBlackout]] = lambda now: (),
        trade_from: datetime | None = None,
    ) -> None:
        """`trade_from`: bars before it are history only (the readers' look-back); the
        clock starts at the first bar at or after it. None: start at the first bar."""
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
        self._model = OptionModel(
            settings.option_model, profiles.instrument, profiles.strategy.options.assumed_delta
        )
        self._price = self._bars[0].open
        self._published: OptionChain | None = None
        self._window_end = -1
        self._window: tuple[Bar, ...] = ()
        self.alarms: list[Alarm] = []

        p = profiles
        self.book = QuoteBook()
        self.broker = SimulatedBroker(self.book, p.execution.quotes.max_age_seconds)
        tracked = TrackedBroker(self.broker)
        self.halt = Halt()
        self.ledger = Ledger(p.risk, p.instrument, StateStore(workdir / "ledger.json"), create=True)
        pipeline = Pipeline(p)
        stops = StopKeeper(p.risk, p.instrument, p.execution)
        executor = Executor(p, pipeline, tracked, stops, self.ledger, self.halt, self.book)
        manager = Manager(p, tracked, stops, self.ledger, self.halt, self.book)
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
            second = 0
            while second < _MINUTE_SECONDS:
                t = bar.ts + timedelta(seconds=second)
                live = self.runner.exposure is not None
                if (
                    second == 0
                    and not live
                    and not self.runner.reconciling
                    and session_at(t, self._p.instrument) is not SessionKind.PRIME
                ):
                    skipped += 1
                    break
                self._price = path_price(bar, second, self.runner.exposure)
                self._publish(t, full_chain=second == 0)
                self.runner.step(t)
                steps += 1
                ended = self._stopped(t)
                if ended is not None or self.runner.exposure is None:
                    break
                second += substep
            if ended is not None:
                break
            if i + 1 < len(bars):
                ended = self._through_gap(bar.ts + _MINUTE, bars[i + 1].ts)
                if ended is not None:
                    break
        if ended is None:
            # The last bar closes too: one more step sees it (and decides on it).
            t = bars[-1].ts + _MINUTE
            self._price = bars[-1].close
            self._publish(t, full_chain=True)
            self.runner.step(t)
            steps += 1
            ended = self._stopped(t)
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
            open_at_end=self.runner.exposure is not None,
            skipped_minutes=skipped,
            steps=steps,
        )

    def _through_gap(self, start: datetime, next_bar: datetime) -> str | None:
        """Missing minutes with a trade live: keep stepping, with no new quotes."""
        t = start
        while t < next_bar and self.runner.exposure is not None:
            self.runner.step(t)
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
