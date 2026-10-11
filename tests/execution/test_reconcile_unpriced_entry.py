"""Delta-3 review (PR #21, claude/night-review-9e22f61.md), N3-1: an entry the broker
reports FILLED is never dropped by reconcile. While its price is pending (IBKR's
orderStatus can run ahead of execDetails and of the position report), reconcile keeps
the entry, never cancels its attached stop and never agrees "flat" over it; it sends
nothing. Past `fill_price_wait_seconds` it halts and alarms CHECK THE FILL once, and
keeps holding. Once the price arrives the entry becomes the position (booked at the
real fill) and reconcile runs with it."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.channels import critical_marker
from footprint.execution.management.malik import Alarm
from footprint.execution.orders import BrokerDown, OrderState, OrderStatus, SimulatedBroker
from footprint.execution.runner import Runner
from tests.decision.scenario import DECISION_TIME
from tests.execution.conftest import Stack
from tests.execution.test_fill_price_pending import Lag
from tests.execution.test_runner import runner_for, through_settle

D = Decimal
SEC = timedelta(seconds=1)
AT_LIMIT = D(151)  # the entry's floor: an ask here fills it


class Scene:
    def __init__(
        self,
        stack: Stack,
        bars: list[Bar],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.stack = stack
        self.runner: Runner
        self.alarms: list[Alarm]
        self.runner, _, self.alarms = runner_for(stack, bars, tmp_path)
        self.lag = Lag(stack.broker, monkeypatch)
        self.runner.step(DECISION_TIME)  # start-up reconcile agrees
        self.runner.step(DECISION_TIME + SEC)  # the entry is sent at its floor, 151
        self.entry = next(o for o in self.lag.placed if o.endswith("-entry"))
        self.stop = next(o for o in self.lag.placed if o.endswith("-stop"))
        self.t = DECISION_TIME + SEC

    def fill(self, *, priced: bool, lagging_report: bool) -> None:
        """The ask comes down to the limit: the entry fills at 151 at the broker."""
        if not priced:
            self.lag.hide(self.entry)
        if lagging_report:
            self.lag.hold({})
        self.t += SEC
        self.stack.quote(150, D(151), self.t)
        assert self.stack.broker.status(self.entry).state is OrderState.FILLED

    def step(self, seconds: int = 1, ask: Decimal = AT_LIMIT) -> None:
        for _ in range(seconds):
            self.t += SEC
            self.stack.quote(150, ask, self.t)
            self.runner.step(self.t)

    def messages(self) -> list[str]:
        return [a.message for a in self.alarms]

    def stop_untouched(self) -> None:
        assert self.stack.broker.status(self.stop).state is OrderState.WORKING
        assert self.stop not in self.lag.cancelled
        assert self.lag.sells() == []
        assert not any(o.endswith("-stop-2") for o in self.lag.placed)
        # What the simulated broker really holds (the report may be held back).
        assert SimulatedBroker.positions(self.stack.broker) == {self.stack.contract: 1}

    def adopted(self) -> None:
        position = self.runner.position
        assert position is not None
        assert position.entry_price == D(151)  # the real fill, never a guess
        assert position.stop_order_id == self.stop
        assert self.stack.ledger.open_position_id == position.position_id
        assert self.stack.broker.status(self.stop).state is OrderState.WORKING


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _wait(stack: Stack) -> int:
    return stack.profiles.execution.reconcile.fill_price_wait_seconds


def _past_everything(stack: Stack) -> int:
    """Longer than the price wait plus every settle wait reconcile could run."""
    r = stack.profiles.execution.reconcile
    return r.fill_price_wait_seconds + 2 * r.max_settle_seconds + 4 * r.settle_seconds + 5


def test_past_the_wait_reconcile_keeps_the_entry_and_its_stop_with_a_lagging_report(
    scene: Scene,
) -> None:
    """The delta-3 probe P1: the price never comes and the position report still shows
    nothing. Reconcile must not cancel the stop or agree "flat"."""
    scene.fill(priced=False, lagging_report=True)
    scene.step(_past_everything(scene.stack))
    scene.stop_untouched()
    assert scene.runner.position is None  # nothing booked without a price
    assert scene.runner.reconciling  # never "agreed" flat over a filled entry
    assert scene.stack.halt.tripped
    messages = scene.messages()
    assert sum("CHECK THE FILL" in m for m in messages) == 1  # once, not every step
    (check,) = [m for m in messages if "CHECK THE FILL" in m]
    assert critical_marker(check) is not None  # it rings Andre's phone
    # Grind review G-2: a hand flatten must not leave the stop live (a naked sell).
    assert "cancel the entry's stop at IBKR first, then flatten by hand" in check
    assert not any("reconcile mismatch" in m for m in messages)
    scene.lag.reveal()  # the price and the holding finally arrive
    scene.step()
    scene.adopted()
    through_settle(scene.stack, scene.runner, scene.t + SEC, (150, 151))
    assert not scene.runner.reconciling  # it agrees with the position it now holds
    assert scene.stack.broker.status(scene.stop).state is OrderState.WORKING


def test_past_the_wait_reconcile_keeps_the_entry_and_its_stop_with_a_true_report(
    scene: Scene,
) -> None:
    """The report shows the contract, but the system has no price: still never flattened
    as "unrecorded" (its P&L could never be booked) and its stop is kept."""
    scene.fill(priced=False, lagging_report=False)
    scene.step(_past_everything(scene.stack))
    scene.stop_untouched()
    assert scene.runner.reconciling
    assert not any("never recorded" in m for m in scene.messages())
    scene.lag.reveal()
    scene.step()
    scene.adopted()


@pytest.mark.parametrize("lagging_report", [False, True])
def test_a_doubt_during_the_price_wait_keeps_the_entry_and_books_it(
    scene: Scene, lagging_report: bool
) -> None:
    """The likelier case: any doubt (a resync, a disconnect) inside the wait."""
    scene.fill(priced=False, lagging_report=lagging_report)
    scene.step()  # the fill is seen, its price pending
    assert scene.runner.position is None
    assert not scene.runner.reconciling
    scene.runner.doubt()
    scene.step(_wait(scene.stack) - 2)  # still inside the wait
    scene.stop_untouched()
    assert scene.runner.reconciling
    assert not scene.stack.halt.tripped  # inside the wait: nothing is wrong yet
    assert not any("never recorded" in m for m in scene.messages())
    assert not any("reconcile mismatch" in m for m in scene.messages())
    scene.lag.reveal()
    scene.step()
    scene.adopted()
    through_settle(scene.stack, scene.runner, scene.t + SEC, (150, 151))
    assert not scene.runner.reconciling
    assert not any("reconcile mismatch" in m for m in scene.messages())


def test_an_entry_that_filled_with_its_price_when_a_doubt_arrives_is_adopted(
    scene: Scene,
) -> None:
    """A doubt on the very step the (priced) fill would have been seen: the fill is
    the position, booked at its price, never flattened as unrecorded."""
    scene.fill(priced=True, lagging_report=False)
    scene.runner.doubt()
    scene.step()
    scene.adopted()
    assert scene.lag.sells() == []
    assert not any("never recorded" in m for m in scene.messages())
    through_settle(scene.stack, scene.runner, scene.t + SEC, (150, 151))
    assert not scene.runner.reconciling


def test_a_doubt_with_an_unfilled_entry_still_cancels_it_and_its_stop(scene: Scene) -> None:
    """Unchanged: a working entry with nothing filled is cancelled and forgotten."""
    scene.runner.doubt()
    scene.step(ask=D(160))  # the market is away from the entry's limit
    assert scene.stack.broker.status(scene.entry).state is OrderState.CANCELLED
    assert scene.stack.broker.status(scene.stop).state is not OrderState.WORKING
    assert scene.runner.position is None
    assert not scene.runner.reconciling
    assert scene.stack.broker.positions() == {}


def _status_fails_once_for(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception] = ConnectionError,
) -> list[int]:
    """The next status question about the entry raises (a lost answer)."""
    asked = scene.stack.broker.status
    failures = [1]

    def flaky(order_id: str) -> OrderStatus:
        if order_id == scene.entry and failures[0]:
            failures[0] -= 1
            raise error("the answer was lost")
        return asked(order_id)

    monkeypatch.setattr(scene.stack.broker, "status", flaky)
    return failures


def test_a_lost_answer_about_a_filled_entry_never_drops_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known filled (price pending): a failed status question is a failed step (halt,
    reconcile again), never a reason to forget the entry and run reconcile over it."""
    scene.fill(priced=False, lagging_report=True)
    scene.step()  # the fill is seen, its price pending
    failures = _status_fails_once_for(scene, monkeypatch)
    scene.runner.doubt()
    scene.step()
    assert failures == [0]  # the question was asked, and failed
    assert scene.stack.halt.tripped
    scene.step(3)
    scene.stop_untouched()
    assert scene.runner.reconciling
    scene.lag.reveal()
    scene.step()
    scene.adopted()


@pytest.mark.parametrize("error", [ConnectionError, BrokerDown])
def test_a_lost_answer_about_an_unfilled_entry_keeps_it_until_the_broker_answers(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Grind review G-1: with no answer, the system cannot know the entry did not
    fill. It is kept (nothing is sent, reconcile does not run over it) and asked
    again on the next step; once the broker answers "working", it is cancelled."""
    failures = _status_fails_once_for(scene, monkeypatch, error)
    scene.runner.doubt()
    scene.step(ask=D(160))
    assert failures == [0]
    assert scene.entry not in scene.lag.cancelled  # nothing sent on a lost answer
    assert scene.stop not in scene.lag.cancelled
    assert scene.runner.reconciling
    scene.step(ask=D(160))  # the broker answers: still working, so it is cancelled
    assert scene.stack.broker.status(scene.entry).state is OrderState.CANCELLED
    assert scene.stack.broker.status(scene.stop).state is not OrderState.WORKING
    assert scene.runner.position is None
    assert scene.stack.broker.positions() == {}


@pytest.mark.parametrize("error", [ConnectionError, BrokerDown])
@pytest.mark.parametrize("lagging_report", [False, True])
def test_a_lost_first_answer_about_an_unseen_unpriced_fill_never_drops_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, error: type[Exception], lagging_report: bool
) -> None:
    """Grind review G-1 (probe PA): the entry fills (price pending) and a doubt arrives
    before the runner has seen the fill; the first status question fails. The entry
    must be kept and its attached stop never cancelled; it is adopted at the real
    price once that arrives."""
    scene.fill(priced=False, lagging_report=lagging_report)
    failures = _status_fails_once_for(scene, monkeypatch, error)
    scene.runner.doubt()
    scene.step()
    assert failures == [0]
    scene.step(3)
    scene.stop_untouched()
    assert scene.runner.position is None
    assert scene.runner.reconciling
    assert not any("reconcile mismatch" in m for m in scene.messages())
    assert not any("never recorded" in m for m in scene.messages())
    scene.lag.reveal()
    scene.step()
    scene.adopted()
    through_settle(scene.stack, scene.runner, scene.t + SEC, (150, 151))
    assert not scene.runner.reconciling


def test_an_entry_whose_cancel_is_not_confirmed_is_left_to_reconcile(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grind review G-3 (V1): settle's cancel is not confirmed (the broker still says
    working, nothing filled). The entry is not held over reconcile: reconcile runs on
    the same step and cancels it from the broker's list once that holds still."""
    cancel = scene.stack.broker.cancel
    ignored = [1]

    def deaf(order_id: str) -> OrderStatus:
        if order_id == scene.entry and ignored[0]:
            ignored[0] -= 1
            return scene.stack.broker.status(order_id)  # not confirmed: still working
        return cancel(order_id)

    monkeypatch.setattr(scene.stack.broker, "cancel", deaf)
    scene.runner.doubt()
    scene.step(ask=D(160))
    assert ignored == [0]
    assert scene.runner.exposure is None  # the entry is not held over reconcile
    scene.step(_past_everything(scene.stack), ask=D(160))
    assert scene.stack.broker.status(scene.entry).state is OrderState.CANCELLED  # by reconcile
    assert scene.runner.position is None
    assert not scene.runner.reconciling
    assert scene.lag.sells() == []


@pytest.mark.parametrize("priced", [True, False])
def test_an_entry_that_fills_as_settle_cancels_it_is_never_dropped(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, priced: bool
) -> None:
    """Grind review G-3 (V2): the entry is working when settle asks, and fills while
    its cancel is on the way. The broker's word after the cancel decides: priced, it
    is the position; price pending, it is kept with its stop."""
    cancel = scene.stack.broker.cancel
    raced = [1]

    def racing(order_id: str) -> OrderStatus:
        if order_id == scene.entry and raced[0]:
            raced[0] -= 1
            scene.fill(priced=priced, lagging_report=not priced)  # fills first
        return cancel(order_id)

    monkeypatch.setattr(scene.stack.broker, "cancel", racing)
    scene.runner.doubt()
    scene.step(ask=D(160))  # the entry is working when the doubt is settled
    assert raced == [0]
    assert not any("never recorded" in m for m in scene.messages())
    if priced:
        scene.adopted()
        assert scene.lag.sells() == []
        return
    scene.stop_untouched()
    assert scene.runner.position is None
    assert scene.runner.reconciling
    scene.lag.reveal()
    scene.step()
    scene.adopted()


def test_a_doubt_inside_the_wait_that_outlasts_it_halts_and_says_check_the_fill(
    scene: Scene,
) -> None:
    """The wait runs out while reconcile already holds the entry (no step failed
    first): reconcile itself halts and alarms CHECK THE FILL once, and keeps holding."""
    scene.fill(priced=False, lagging_report=True)
    scene.step()  # the fill is seen, its price pending
    scene.runner.doubt()
    scene.step()  # reconcile now holds the entry
    assert not scene.stack.halt.tripped
    scene.step(_past_everything(scene.stack))
    assert scene.stack.halt.tripped
    messages = scene.messages()
    assert sum("CHECK THE FILL" in m for m in messages) == 1
    assert not any("step failed" in m for m in messages)  # reconcile's own hold, not a crash
    scene.stop_untouched()
    assert scene.runner.reconciling
