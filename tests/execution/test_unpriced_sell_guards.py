"""Delta review 3, N3-4 (reviewer mutants V2, V3, V4): the guards that decide which
filled-but-unpriced order counts as this position's sale. Only a SELL of the
position's own contract is its sale (D40). A buy, or a sell of another contract, is
never mistaken for it, and a hold on the position's contract never stops reconcile
from flattening a different contract."""

from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.execution.orders import (
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Quote,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import filled_position
from tests.execution.test_fill_price_pending import Lag, _exit_filled_unpriced, _wait

D = Decimal
SEC = timedelta(seconds=1)
LEFTOVER = "NQ-leftover"


def _filled_unpriced(stack: Stack, order_id: str, contract_id: str, side: Side) -> None:
    """The broker reports `order_id` FILLED with no price yet."""
    order = Order(order_id, contract_id, side, 1, OrderType.LIMIT, D(150))
    stack.broker._orders[order_id] = OrderStatus(order, OrderState.FILLED, 1, None)


def test_a_filled_buy_of_the_position_is_never_its_sale(stack: Stack) -> None:
    """V2: a reconcile BUY recorded for the position (buying back a short), FILLED with
    its price pending, is not the position's sale: booking it as one would close the
    position at the buy's price."""
    position = filled_position(stack)
    pid = position.position_id
    stack.manager._reconcile_orders[pid] = ["reconcile-buy"]
    _filled_unpriced(stack, "reconcile-buy", position.contract_id, Side.BUY)
    assert stack.manager._unpriced_sell(position) is None
    stack.manager._reconcile_orders[pid] = ["reconcile-buy", "reconcile-sell"]
    _filled_unpriced(stack, "reconcile-sell", position.contract_id, Side.SELL)
    assert stack.manager._unpriced_sell(position) == "reconcile-sell"  # the control


def test_a_filled_sell_of_another_contract_is_never_the_positions_sale(stack: Stack) -> None:
    """V3: a reconcile sell recorded for the position but for another contract (an
    unrecorded holding flattened while the position was open) is not this position's
    sale: counting it would leave the position's own contract unsold."""
    position = filled_position(stack)
    pid = position.position_id
    stack.manager._reconcile_orders[pid] = ["reconcile-other"]
    _filled_unpriced(stack, "reconcile-other", LEFTOVER, Side.SELL)
    assert stack.manager._unpriced_sell(position) is None


def test_a_filled_sell_of_another_contract_never_stops_reconcile_selling_the_position(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3 at reconcile level: an extra holding is flattened first and its sell fills
    with its price pending. The position's own contract is still held, with no sale of
    its own known: reconcile flattens it in the same round."""
    position = filled_position(stack)
    sim = stack.broker
    lag = Lag(sim, monkeypatch)
    sim._positions = {LEFTOVER: 1, **sim._positions}  # the leftover comes first
    t = SEND + 60 * SEC
    stack.book.publish(Quote(LEFTOVER, D(40), D(41), t))
    lag.hidden = _EndsWith(f"-{LEFTOVER}")  # every sell of the leftover: price pending
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(4):
        now = t + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(LEFTOVER, D(40), D(41), now))
        stack.manager.reconcile(position, now)
    sent = [o for o in lag.placed if o.startswith("reconcile-")]
    assert [o for o in sent if o.endswith(f"-{LEFTOVER}")]
    assert [o for o in sent if o.endswith(f"-{position.contract_id}")]  # sold as well
    assert SimulatedBroker.positions(sim) == {}


class _EndsWith(set[str]):
    def __init__(self, suffix: str) -> None:
        super().__init__()
        self.suffix = suffix

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and item.endswith(self.suffix)


def test_a_hold_on_the_positions_contract_never_stops_flattening_another_contract(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4: the position's exit filled with its price pending, so its own contract is
    held off (D40). An extra holding of another contract is not part of that sale and
    is flattened as usual, inside the price wait."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    sim = stack.broker
    sim._positions[LEFTOVER] = 1
    monkeypatch.setattr(
        sim,
        "positions",
        lambda: {**SimulatedBroker.positions(sim), position.contract_id: position.contracts},
    )
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    rounds = 4
    assert rounds * (settle + SEC) < _wait(stack)  # all inside the price wait
    for k in range(rounds):
        now = t + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.book.publish(Quote(LEFTOVER, D(40), D(41), now))
        stack.manager.reconcile(position, now)
    sent = [o for o in lag.placed if o.startswith("reconcile-")]
    assert sent
    assert all(o.endswith(f"-{LEFTOVER}") for o in sent)  # never the held one
    assert LEFTOVER not in SimulatedBroker.positions(sim)
    assert lag.sells().count(f"{position.position_id}-exit-1") == 1
