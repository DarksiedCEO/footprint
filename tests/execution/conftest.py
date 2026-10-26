from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.pipeline import ApprovedEntry, Pipeline
from footprint.decision.session import NewsBlackout
from footprint.decision.stop import StopKeeper
from footprint.execution.executor.diego import Executor, Position
from footprint.execution.halt import Halt
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import Alarm, Closed, Manager
from footprint.execution.market import QuoteBook
from footprint.execution.orders import Quote, SimulatedBroker, TrackedBroker
from tests.conftest import REPO_ROOT
from tests.decision.plays import open_play
from tests.decision.scenario import DECISION_TIME, chain_for, market

D = Decimal


@pytest.fixture(scope="session")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="session")
def bars() -> list[Bar]:
    return market()


@dataclass
class Stack:
    profiles: Profiles
    pipeline: Pipeline
    broker: SimulatedBroker
    stops: StopKeeper
    ledger: Ledger
    executor: Executor
    manager: Manager
    approved: ApprovedEntry
    alarms: list[Alarm]
    state_path: Path
    halt: Halt
    book: QuoteBook

    @property
    def contract(self) -> str:
        return self.approved.plan.choice.quote.contract_id

    def quote(self, bid: float | Decimal, ask: float | Decimal, ts: datetime) -> Quote:
        q = Quote(self.contract, D(str(bid)), D(str(ask)), ts)
        self.book.publish(q)
        return q

    def step(
        self,
        position: Position,
        _published: Quote,
        blackouts: Iterable[NewsBlackout],
        now: datetime,
    ) -> Closed | None:
        """Manage on the book's latest quote (the one `stack.quote` just published)."""
        return self.manager.manage(position, blackouts, now)

    def use_broker(self, broker: SimulatedBroker) -> TrackedBroker:
        """Swap in another simulator (wrapped in the order record) everywhere."""
        tracked = TrackedBroker(broker)
        self.executor._broker = tracked
        self.manager._broker = tracked
        self.broker = broker
        return tracked

    def reconcile(self, position: Position | None, _published: Quote, now: datetime) -> bool:
        """Reconcile through its two settle waits, as the runner's steps would: the
        first look, the disagreement holding still, then what is held holding still.
        Returns the last answer; the last call is at `now + 2 * settle_seconds`."""
        settle = timedelta(seconds=self.profiles.execution.reconcile.settle_seconds)
        agreed = False
        for k in range(3):
            agreed = self.manager.reconcile(position, now + k * settle)
            if agreed:
                break
        return agreed


@pytest.fixture
def stack(profiles: Profiles, bars: list[Bar], tmp_path: Path) -> Stack:
    pipeline = Pipeline(profiles, play=open_play())
    store_path = tmp_path / "ledger.json"
    ledger = Ledger(profiles.risk, profiles.instrument, StateStore(store_path), create=True)
    decision = pipeline.decide(
        bars, chain_for(bars[-1].close), ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    assert decision.approved is not None, decision.reasons()
    book = QuoteBook()
    broker = SimulatedBroker(book, profiles.execution.quotes.max_age_seconds)
    stops = StopKeeper(profiles.risk, profiles.instrument, profiles.execution)
    alarms: list[Alarm] = []
    halt = Halt()
    tracked = TrackedBroker(broker)
    executor = Executor(profiles, pipeline, tracked, stops, ledger, halt, book)
    manager = Manager(profiles, tracked, stops, ledger, halt, book, alarms.append)
    s = Stack(
        profiles,
        pipeline,
        broker,
        stops,
        ledger,
        executor,
        manager,
        decision.approved,
        alarms,
        store_path,
        halt,
        book,
    )
    s.quote(150, 152, DECISION_TIME)
    return s


SEND = DECISION_TIME + timedelta(seconds=1)
