"""Grind review of e56a2ab (PR #21, claude/grind-review-e56a2ab.md), test gaps G1-1 and
G1-2 (LOW; the code is right, nothing pinned it).

G1-1: settle asks the broker about a working entry, cancels it, and asks again. When
the entry fills while that cancel is on the way and the SECOND question is lost, the
entry must be kept: its attached stop stays at the broker, nothing is sold, and the
fill is adopted at its real price once that arrives. Dropping the ticket there
(reviewer mutant R1) lets reconcile cancel the stop over a held contract.

G1-2: a BrokerDown during settle waits for the broker (D39): no halt, and no CHECK THE
FILL alarm about an entry that may never have filled (reviewer mutant R3 handled it
like FillUnpriced). Any other lost answer is a failed step (a halt), never CHECK THE
FILL either."""

from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.orders import BrokerDown, OrderStatus
from tests.execution.conftest import Stack
from tests.execution.test_reconcile_unpriced_entry import Scene, _status_fails_once_for

D = Decimal


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _check_the_fill(scene: Scene) -> list[str]:
    return [m for m in scene.messages() if "CHECK THE FILL" in m]


def _no_halt_on_broker_down(scene: Scene, error: type[Exception]) -> None:
    """D39: BrokerDown waits for the broker; anything else is a failed step (a halt)."""
    if error is BrokerDown:
        assert not scene.stack.halt.tripped
    else:
        assert scene.stack.halt.tripped
    assert _check_the_fill(scene) == []


@pytest.mark.parametrize("error", [ConnectionError, BrokerDown])
def test_a_lost_answer_after_settles_cancel_while_the_entry_fills_never_drops_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Probe PD of the e56a2ab review: the entry is working when settle asks, it fills
    (price hidden, the position report held at nothing) while the cancel is on the
    way, and the status question AFTER the cancel is lost."""
    cancel, status = scene.stack.broker.cancel, scene.stack.broker.status
    state = {"raced": 1, "fail_next": 0}

    def racing(order_id: str) -> OrderStatus:
        if order_id == scene.entry and state["raced"]:
            state["raced"] = 0
            scene.fill(priced=False, lagging_report=True)  # fills before the cancel lands
            state["fail_next"] = 1
        return cancel(order_id)

    def flaky(order_id: str) -> OrderStatus:
        if order_id == scene.entry and state["fail_next"]:
            state["fail_next"] = 0
            raise error("lost after the cancel")
        return status(order_id)

    monkeypatch.setattr(scene.stack.broker, "cancel", racing)
    monkeypatch.setattr(scene.stack.broker, "status", flaky)
    scene.runner.doubt()
    scene.step(ask=D(160))  # the entry is working when the doubt is settled
    assert state == {"raced": 0, "fail_next": 0}  # it raced, and the second answer was lost
    assert scene.runner.exposure is not None  # the entry is kept, not dropped
    _no_halt_on_broker_down(scene, error)
    scene.step(3)
    scene.stop_untouched()  # the stop rests, nothing is sold, the contract is held
    assert scene.runner.position is None
    assert scene.runner.reconciling
    assert not any("reconcile mismatch" in m for m in scene.messages())
    assert not any("never recorded" in m for m in scene.messages())
    _no_halt_on_broker_down(scene, error)
    scene.lag.reveal()
    scene.step()
    scene.adopted()  # booked at the real fill, 151, with its own stop
    assert scene.lag.sells() == []


@pytest.mark.parametrize("error", [ConnectionError, BrokerDown])
def test_a_lost_answer_about_an_unfilled_entry_never_says_check_the_fill(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """G1-2: the entry never filled and the first settle question is lost. BrokerDown
    waits (no halt); neither error tells Andre to check a fill that never happened."""
    failures = _status_fails_once_for(scene, monkeypatch, error)
    scene.runner.doubt()
    scene.step(ask=D(160))
    assert failures == [0]
    _no_halt_on_broker_down(scene, error)
    scene.step(ask=D(160))  # the broker answers: still working, so it is cancelled
    assert scene.runner.position is None
    assert scene.lag.sells() == []
    assert _check_the_fill(scene) == []


@pytest.mark.parametrize("lagging_report", [False, True])
def test_broker_down_during_settle_of_an_unseen_unpriced_fill_waits_without_a_halt(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, lagging_report: bool
) -> None:
    """G1-2: the entry filled (price pending) before the runner saw it, and the first
    settle question meets BrokerDown: wait for the broker, no halt, no CHECK THE FILL;
    the entry is adopted at its real price once that arrives."""
    scene.fill(priced=False, lagging_report=lagging_report)
    failures = _status_fails_once_for(scene, monkeypatch, BrokerDown)
    scene.runner.doubt()
    scene.step()
    assert failures == [0]
    _no_halt_on_broker_down(scene, BrokerDown)
    scene.step(3)
    scene.stop_untouched()
    _no_halt_on_broker_down(scene, BrokerDown)
    scene.lag.reveal()
    scene.step()
    scene.adopted()
