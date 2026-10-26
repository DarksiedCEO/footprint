"""Delta-3 review (PR #21, claude/night-review-9e22f61.md), N3-5: while an entry the
broker reports FILLED waits for its price, its attached stop is checked every step,
as it is while the entry works (H-2). The contract is held during the wait, so a stop
that is gone must not wait up to `fill_price_wait_seconds` (or forever, past it) to
be seen:

- the stop is protecting, or has filled: nothing to do;
- the stop is live but not protecting (held back, or its cancel pending): it can
  still sell, so nothing is placed beside it (AEGIS K-2); halt and alarm;
- the stop is finished without filling (cancelled, rejected): the contract is held
  with no stop at the broker, so ONE new stop is placed at the attached stop's
  price (priced from the entry's limit, the highest it could have filled at; no
  fill price is needed and nothing is booked). The entry keeps waiting for its price
  and the new stop is the one the position adopts. If that stop is refused too,
  nothing more is placed: halt and alarm "protect it by hand".

The same check runs on the settle path (a doubt during the wait) and past the wait."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.core.models import Bar
from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import Stack
from tests.execution.test_reconcile_unpriced_entry import Scene, _past_everything
from tests.execution.test_runner import through_settle

D = Decimal
SEC = timedelta(seconds=1)


@pytest.fixture
def scene(stack: Stack, bars: list[Bar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scene:
    return Scene(stack, bars, tmp_path, monkeypatch)


def _pid(scene: Scene) -> str:
    return scene.entry.removesuffix("-entry")


def _replacements(scene: Scene) -> list[str]:
    """Every stop placed for the position after the attached one."""
    return [o for o in scene.lag.placed if o.startswith(_pid(scene) + "-stop-")]


def _broker_cancels_the_stop(scene: Scene) -> None:
    """IBKR cancels the attached stop on its own (it is not the system's cancel)."""
    SimulatedBroker.cancel(scene.stack.broker, scene.stop)
    assert scene.stack.broker.status(scene.stop).state is OrderState.CANCELLED


def _held_back(scene: Scene, monkeypatch: pytest.MonkeyPatch, order_id: str) -> None:
    """The stop is reported live but held back (IBKR Inactive)."""
    original = scene.stack.broker.status

    def marked(asked: str) -> OrderStatus:
        status = original(asked)
        if asked == order_id and status.state is OrderState.WORKING:
            return replace(status, held_back=True)
        return status

    monkeypatch.setattr(scene.stack.broker, "status", marked)


def _halt_text(scene: Scene) -> str:
    return " | ".join(scene.stack.halt.reasons)


def test_a_stop_the_broker_cancels_during_the_wait_is_replaced_once(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step()  # the fill is seen; its price is pending
    old = scene.stack.broker.status(scene.stop).order
    assert _replacements(scene) == []
    _broker_cancels_the_stop(scene)
    scene.step()
    (new_id,) = _replacements(scene)
    new = scene.stack.broker.status(new_id)
    assert new.state is OrderState.WORKING
    assert new.protects
    order = new.order
    assert order.side is Side.SELL
    assert order.order_type is OrderType.STOP_LIMIT
    assert order.quantity == 1
    assert order.contract_id == scene.stack.contract
    assert order.oca_group == _pid(scene)  # the position's group: exits cancel it
    assert order.parent_id is None
    # The attached stop's own price: from the entry's limit, no fill price needed.
    assert (order.stop_price, order.limit_price) == (old.stop_price, old.limit_price)
    scene.step(5)
    assert _replacements(scene) == [new_id]  # once, never one per step
    assert scene.lag.sells() == []
    assert scene.runner.position is None  # nothing booked without a price
    assert not scene.stack.halt.tripped  # protection restored, as on the fill (_after)
    scene.lag.reveal()
    scene.step()
    position = scene.runner.position
    assert position is not None
    assert position.entry_price == D(151)  # the real fill
    assert position.stop_order_id == new_id  # the position adopts the new stop
    assert _replacements(scene) == [new_id]  # no second stop beside it on the fill
    assert scene.stack.broker.status(new_id).protects


def test_a_cancelled_stop_is_replaced_on_the_settle_path_too(scene: Scene) -> None:
    """A doubt during the wait: reconcile settles the entry every step instead of
    on_time, and the stop is checked there too."""
    scene.fill(priced=False, lagging_report=True)
    scene.step()
    scene.runner.doubt()
    _broker_cancels_the_stop(scene)
    scene.step()
    (new_id,) = _replacements(scene)
    assert scene.stack.broker.status(new_id).protects
    scene.step(3)
    assert _replacements(scene) == [new_id]
    assert scene.runner.reconciling  # still holding off reconcile over the entry
    assert scene.lag.sells() == []
    assert SimulatedBroker.positions(scene.stack.broker) == {scene.stack.contract: 1}


def test_a_stop_cancelled_past_the_wait_is_replaced(scene: Scene) -> None:
    """Past the wait the runner keeps settling the entry (CHECK THE FILL): a stop
    lost then is still replaced, not left for Andre to notice."""
    scene.fill(priced=False, lagging_report=True)
    scene.step(_past_everything(scene.stack))
    assert any("CHECK THE FILL" in m for m in scene.messages())
    assert _replacements(scene) == []
    _broker_cancels_the_stop(scene)
    scene.step()
    (new_id,) = _replacements(scene)
    assert scene.stack.broker.status(new_id).protects
    assert scene.lag.sells() == []
    scene.lag.reveal()
    scene.step()
    assert scene.runner.position is not None
    assert scene.runner.position.stop_order_id == new_id
    through_settle(scene.stack, scene.runner, scene.t + SEC, (150, 151))
    assert scene.stack.broker.status(new_id).protects  # reconcile keeps the new stop


def test_a_held_back_stop_during_the_wait_is_never_doubled(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    _held_back(scene, monkeypatch, scene.stop)
    scene.step(4)
    assert _replacements(scene) == []  # it can still sell: nothing beside it (K-2)
    assert scene.lag.sells() == []
    assert scene.stack.halt.tripped
    text = _halt_text(scene)
    assert scene.stop in text
    assert "held back" in text
    assert any(scene.stop in m and "held back" in m for m in scene.messages())


def test_a_stop_being_cancelled_during_the_wait_is_never_doubled(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    original = scene.stack.broker.status

    def cancelling(asked: str) -> OrderStatus:
        status = original(asked)
        if asked == scene.stop and status.state is OrderState.WORKING:
            return replace(status, cancelling=True)
        return status

    monkeypatch.setattr(scene.stack.broker, "status", cancelling)
    scene.step(3)
    assert _replacements(scene) == []
    assert scene.stack.halt.tripped
    text = _halt_text(scene)
    assert scene.stop in text
    assert "being cancelled" in text


def test_a_refused_replacement_is_not_retried_and_says_protect_by_hand(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    placing = scene.stack.broker.place

    def refusing(order: Order) -> OrderStatus:
        status = placing(order)
        if order.order_id.startswith(_pid(scene) + "-stop-"):
            SimulatedBroker.cancel(scene.stack.broker, order.order_id)
            return replace(status, state=OrderState.REJECTED)
        return status

    monkeypatch.setattr(scene.stack.broker, "place", refusing)
    _broker_cancels_the_stop(scene)
    scene.step(5)
    assert len(_replacements(scene)) == 1  # one try, never one per step
    assert scene.lag.sells() == []
    assert scene.stack.halt.tripped
    text = _halt_text(scene)
    assert "no resting stop" in text
    assert "by hand" in text
    assert any("no resting stop" in m for m in scene.messages())


def test_a_stop_that_filled_during_the_wait_is_left_alone(scene: Scene) -> None:
    """The attached stop triggered and filled: the position is already sold. A new
    stop would be a second sell (a short)."""
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    stop = scene.stack.broker.status(scene.stop)
    assert stop.order.stop_price is not None
    low = stop.order.stop_price - D(1)
    scene.t += SEC
    scene.stack.quote(float(low - D(1)), low, scene.t)  # the bid falls through the stop
    assert scene.stack.broker.status(scene.stop).state is OrderState.FILLED
    for _ in range(3):
        scene.t += SEC
        scene.stack.quote(float(low - D(1)), low, scene.t)
        scene.runner.step(scene.t)
    assert _replacements(scene) == []
    assert scene.lag.sells() == []
    assert not any("no resting stop" in r for r in scene.stack.halt.reasons)
    assert SimulatedBroker.positions(scene.stack.broker) == {}  # flat, never short


def test_a_protecting_stop_during_the_wait_is_left_alone(scene: Scene) -> None:
    scene.fill(priced=False, lagging_report=False)
    scene.step(4)
    assert _replacements(scene) == []
    assert not scene.stack.halt.tripped
    scene.stop_untouched()


def test_after_a_refused_replacement_the_fill_places_a_stop_under_a_new_id(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement was refused; when the price arrives the fill path places its
    own new stop (as it does for any attached stop that is gone), under an id never
    used before: the position ends protected."""
    scene.fill(priced=False, lagging_report=False)
    scene.step()
    placing = scene.stack.broker.place
    refused: list[str] = []

    def refusing_once(order: Order) -> OrderStatus:
        status = placing(order)
        if order.order_id.startswith(_pid(scene) + "-stop-") and not refused:
            refused.append(order.order_id)
            SimulatedBroker.cancel(scene.stack.broker, order.order_id)
            return replace(status, state=OrderState.REJECTED)
        return status

    monkeypatch.setattr(scene.stack.broker, "place", refusing_once)
    _broker_cancels_the_stop(scene)
    scene.step(2)
    assert len(refused) == 1
    scene.lag.reveal()
    scene.step()
    position = scene.runner.position
    assert position is not None
    assert position.entry_price == D(151)
    assert position.stop_order_id is not None
    assert position.stop_order_id not in (scene.stop, refused[0])
    assert scene.stack.broker.status(position.stop_order_id).protects
    stops = _replacements(scene)
    assert len(stops) == len(set(stops)) == 2  # every id used once
