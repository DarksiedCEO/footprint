"""D35, as Andre decided it (remediation item 6): while IBKR is lost, no new entry and
no order of any kind; resting protective orders stay untouched; trading resumes only
after the broker is back AND a reconcile agrees."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from footprint.core.models import Bar
from footprint.execution.journal.josephine import Journal
from footprint.execution.orders import BrokerDown, OrderState, OrderType, SimulatedBroker
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_runner import runner_for

D = Decimal
SEC = timedelta(seconds=1)
_CALLS = ("place", "modify", "cancel", "status", "positions", "open_orders")


class Outage:
    """IBKR unreachable: every call raises BrokerDown (and is counted), `ready` is
    False. The simulated exchange keeps running: a resting stop can still fill."""

    def __init__(self, broker: SimulatedBroker) -> None:
        self.broker = broker
        self.attempts: list[str] = []
        self._saved: dict[str, Any] = {}

    def start(self) -> None:
        self.broker.ready = False  # type: ignore[attr-defined]
        for name in _CALLS:
            self._saved[name] = getattr(self.broker, name)

            def down(*args: Any, _name: str = name, **kwargs: Any) -> Any:
                self.attempts.append(_name)
                raise BrokerDown("IBKR connection lost")

            setattr(self.broker, name, down)

    def end(self) -> None:
        for name, method in self._saved.items():
            setattr(self.broker, name, method)
        self.broker.ready = True  # type: ignore[attr-defined]

    def resting(self) -> list[Any]:
        """What really rests at the exchange (read past the outage)."""
        return [s.order for s in self.broker._orders.values() if s.state is OrderState.WORKING]


def holding(stack: Stack, bars: list[Bar], tmp_path: Path) -> tuple[Runner, Journal, datetime]:
    """A runner holding one position with its resting stop (as in the one-trade test)."""
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    for second in range(1, 12):
        runner.step(DECISION_TIME + second * SEC)
    fill = DECISION_TIME + 12 * SEC
    stack.quote(150, D("151.5"), fill)
    runner.step(fill)
    assert runner.position is not None
    assert [o.order_type for o in stack.broker.open_orders()] == [OrderType.STOP_LIMIT]
    return runner, journal, fill


def test_flat_a_loss_blocks_every_entry_and_back_needs_a_reconcile(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)  # start-up reconcile agrees
    assert not runner.reconciling
    outage = Outage(stack.broker)
    outage.start()
    for second in range(1, 90):  # across a new minute bar: nothing is decided
        runner.step(DECISION_TIME + second * SEC)
    assert journal.decisions() == []
    assert outage.attempts == []  # not even a question was sent
    assert runner.reconciling
    outage.end()
    back = DECISION_TIME + 90 * SEC
    runner.step(back)  # back: reconciles first, decides nothing on this step
    assert journal.decisions() == []
    assert not runner.reconciling
    runner.step(back + SEC)
    assert len(journal.decisions()) == 1


def test_holding_a_loss_sends_nothing_and_keeps_the_resting_stop(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, _, fill = holding(stack, bars, tmp_path)
    alarms_before = len(stack.alarms)
    (stop,) = stack.broker.open_orders()
    outage = Outage(stack.broker)
    outage.start()
    max_unmanaged = stack.profiles.execution.runner.max_unmanaged_seconds
    t = fill
    for second in range(1, 4 * max_unmanaged):  # far past the unmanaged limit
        t = fill + second * SEC
        stack.quote(150, D("151.5"), t)
        runner.step(t)
    assert outage.attempts == []  # no order, cancel, modify or flatten was tried
    assert [o.order_id for o in outage.resting()] == [stop.order_id]  # stop untouched
    assert runner.position is not None
    assert not any("unmanaged" in a.message for a in stack.alarms[alarms_before:])
    outage.end()
    for k in range(1, 4):
        stack.quote(150, D("151.5"), t + k * SEC)
        runner.step(t + k * SEC)  # reconcile agrees, then normal management
    assert not runner.reconciling
    assert runner.position is not None  # not flattened because of the outage
    assert runner.closed == []
    assert [o.order_id for o in stack.broker.open_orders()] == [stop.order_id]


def test_a_stop_filled_during_the_loss_is_found_by_the_reconcile(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """Stale state: the broker moved on while we could not see it."""
    runner, journal, fill = holding(stack, bars, tmp_path)
    outage = Outage(stack.broker)
    outage.start()
    t = fill + SEC
    stack.quote(114, 116, t)  # the exchange fills the resting stop during the outage
    runner.step(t)
    assert runner.position is not None  # we cannot know yet
    assert outage.resting() == []  # (it filled at the exchange)
    outage.end()
    for k in range(1, 15):
        stack.quote(114, 116, t + k * SEC)
        runner.step(t + k * SEC)
    assert runner.position is None
    assert stack.broker.positions() == {}
    assert len(runner.closed) == 1
    assert len(journal.decisions()) == 1  # no new entry was decided on the stale view


def test_a_reconcile_that_never_agrees_never_lets_trading_resume(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    outage = Outage(stack.broker)
    outage.start()
    runner.step(DECISION_TIME + SEC)
    outage.end()
    stack.manager.reconcile = lambda position, now: False  # type: ignore[method-assign]
    for second in range(2, 300):
        runner.step(DECISION_TIME + second * SEC)
    assert runner.reconciling
    assert journal.decisions() == []
    assert [o for o in stack.broker.open_orders() if o.order_type is OrderType.LIMIT] == []


def test_a_doubt_set_while_turning_ready_stops_the_decision_in_flight(
    stack: Stack, bars: list[Bar], tmp_path: Path
) -> None:
    """The adapter sets the doubt just before it turns ready. A step that began before
    the doubt and reads "ready" after it must still not decide: _decide re-checks."""
    runner, journal, _ = runner_for(stack, bars, tmp_path)
    runner.step(DECISION_TIME)
    assert not runner.reconciling

    reads = [0]

    class TurningReady:
        """`ready` as the adapter publishes it mid-step: the step's first read sees it
        ready with no doubt; by the next read (in _decide) the doubt has been set."""

        def __get__(self, broker: object, owner: object) -> bool:
            reads[0] += 1
            if reads[0] >= 2:
                runner.doubt()
            return True

    original = type(stack.broker)
    stack.broker.__class__ = type("Turning", (original,), {"ready": TurningReady()})
    try:
        runner.step(DECISION_TIME + SEC)
    finally:
        stack.broker.__class__ = original
    assert reads[0] >= 2  # the step got as far as _decide
    assert journal.decisions() == []  # nothing decided on the step the doubt landed in
    assert runner.reconciling
