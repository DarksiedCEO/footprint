from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.pipeline import ApprovedEntry, Pipeline
from footprint.decision.stop import StopKeeper
from footprint.execution.executor.diego import Executor
from footprint.execution.ledger import Ledger, StateStore
from footprint.execution.management.malik import Alarm, Manager
from footprint.execution.orders import Quote, SimulatedBroker
from tests.conftest import REPO_ROOT
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

    @property
    def contract(self) -> str:
        return self.approved.plan.choice.quote.contract_id

    def quote(self, bid: float | Decimal, ask: float | Decimal, ts: datetime) -> Quote:
        q = Quote(self.contract, D(str(bid)), D(str(ask)), ts)
        self.broker.on_quote(q)
        return q


@pytest.fixture
def stack(profiles: Profiles, bars: list[Bar], tmp_path: Path) -> Stack:
    pipeline = Pipeline(profiles)
    store_path = tmp_path / "ledger.json"
    ledger = Ledger(profiles.risk, profiles.instrument, StateStore(store_path))
    decision = pipeline.decide(
        bars, chain_for(bars[-1].close), ledger.account(DECISION_TIME), (), DECISION_TIME
    )
    assert decision.approved is not None, decision.reasons()
    broker = SimulatedBroker()
    stops = StopKeeper(profiles.risk, profiles.instrument, profiles.execution)
    alarms: list[Alarm] = []
    executor = Executor(profiles, pipeline, broker, stops, ledger)
    manager = Manager(profiles, broker, stops, ledger, alarms.append)
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
    )
    s.quote(150, 152, DECISION_TIME)
    return s


SEND = DECISION_TIME + timedelta(seconds=1)
