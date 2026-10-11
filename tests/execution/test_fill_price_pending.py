"""Delta review 2 (claude/audit-2026-10-07-delta2.md), N-1: an order the broker reports
FILLED before its price is known (IBKR's orderStatus "Filled" at price 0 ahead of its
execution; the adapter then reports the fill with no price, never a guessed one) is a
fill whose price is pending: never "unfilled", never a reason to cancel its stop or to
send another sell. The price is waited for, bounded by `fill_price_wait_seconds`;
past that the runner halts and reconciles."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from footprint.execution.executor.diego import EntryOutcome, EntryTicket, Position
from footprint.execution.management.malik import ExitReason, NeedsReconcile
from footprint.execution.orders import (
    FillUnpriced,
    Order,
    OrderState,
    OrderStatus,
    OrderType,
    Side,
    SimulatedBroker,
)
from tests.execution.conftest import SEND, Stack
from tests.execution.test_execution import FILLED_AT, exit_rests_on_arrival, filled_position

D = Decimal
SEC = timedelta(seconds=1)


class Lag:
    """The broker as IBKR can report it: an order's fill is known (orderStatus Filled)
    before its price and the new holding are (execDetails, the position report).
    `hide(order_id)` strips the price from every answer about that order; `hold(...)`
    freezes the position report; `reveal()` lets both arrive.

    Weaker than the real adapter (grind review G2-4): a frozen report never disagrees
    with anything here, while `IbkrBroker.positions()` raises PositionsInDoubt once its
    counted executions and IBKR's report disagree. A test that passes on `Lag` does not
    prove reconcile would never re-sell after its own priced fill while the report
    lags; that property rests on the adapter's two-source check."""

    def __init__(self, broker: SimulatedBroker, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hidden: set[str] = set()
        self.held: dict[str, int] | None = None
        self.placed: list[str] = []
        self.cancelled: list[str] = []
        status, place, cancel = broker.status, broker.place, broker.cancel
        modify, positions, attached = broker.modify, broker.positions, broker.place_attached

        def strip(answer: OrderStatus) -> OrderStatus:
            if answer.order.order_id in self.hidden and answer.filled:
                return replace(answer, avg_price=None)
            return answer

        def placing(order: Order) -> OrderStatus:
            self.placed.append(order.order_id)
            return strip(place(order))

        def placing_attached(parent: Order, child: Order) -> tuple[OrderStatus, OrderStatus]:
            self.placed += [parent.order_id, child.order_id]
            first, second = attached(parent, child)
            return strip(first), strip(second)

        monkeypatch.setattr(broker, "status", lambda order_id: strip(status(order_id)))
        monkeypatch.setattr(broker, "place", placing)
        monkeypatch.setattr(broker, "place_attached", placing_attached)

        def cancelling(order_id: str) -> OrderStatus:
            self.cancelled.append(order_id)
            return strip(cancel(order_id))

        monkeypatch.setattr(broker, "cancel", cancelling)
        monkeypatch.setattr(
            broker, "modify", lambda order_id, lim, stop: strip(modify(order_id, lim, stop))
        )
        monkeypatch.setattr(
            broker, "positions", lambda: dict(self.held) if self.held is not None else positions()
        )

    def hide(self, *order_ids: str) -> None:
        self.hidden.update(order_ids)

    def hold(self, held: dict[str, int]) -> None:
        self.held = dict(held)

    def reveal(self) -> None:
        self.hidden.clear()
        self.held = None

    def sells(self) -> list[str]:
        return [o for o in self.placed if "-exit-" in o or o.startswith("reconcile-")]


def _wait(stack: Stack) -> timedelta:
    return timedelta(seconds=stack.profiles.execution.reconcile.fill_price_wait_seconds)


# --- the entry (Diego) ------------------------------------------------------------


def _entry_filled_unpriced(stack: Stack, lag: Lag) -> tuple[EntryTicket, str, str]:
    """Submit and step to the fill at 151.5, with the fill's price withheld."""
    ticket = stack.executor.submit(stack.approved, (), SEND)
    assert ticket.stop_order_id is not None
    lag.hide(ticket.order_id)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stack.quote(150, D("151.5"), FILLED_AT)
    ticket = stack.executor.on_time(ticket, FILLED_AT)
    stop_id = ticket.stop_order_id
    assert stop_id is not None
    return ticket, ticket.order_id, stop_id


def test_an_entry_filled_before_its_price_is_known_keeps_its_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    ticket, entry_id, stop_id = _entry_filled_unpriced(stack, lag)
    assert stack.broker.status(entry_id).state is OrderState.FILLED  # it did fill
    assert ticket.outcome is EntryOutcome.WORKING  # filled, price pending: not "unfilled"
    assert ticket.position is None  # nothing is booked without a price
    assert stack.broker.status(stop_id).state is OrderState.WORKING  # the stop stays
    assert not any("stop" in o and o != stop_id for o in lag.placed)  # no stop beside it
    lag.reveal()
    ticket = stack.executor.on_time(ticket, FILLED_AT + SEC)
    assert ticket.outcome is EntryOutcome.PROTECTED
    assert ticket.position is not None
    assert ticket.position.entry_price == D("151.5")  # the real fill, once reported
    assert ticket.position.stop_order_id == stop_id


def test_an_entry_whose_price_never_comes_halts_for_reconcile_with_its_stop_kept(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    ticket, _, stop_id = _entry_filled_unpriced(stack, lag)
    almost = FILLED_AT + _wait(stack) - SEC
    ticket = stack.executor.on_time(ticket, almost)
    assert ticket.outcome is EntryOutcome.WORKING  # still waiting, inside the bound
    with pytest.raises(FillUnpriced):
        stack.executor.on_time(ticket, FILLED_AT + _wait(stack))
    assert ticket.position is None
    assert stack.broker.status(stop_id).state is OrderState.WORKING  # never cancelled


def test_a_partly_filled_entry_cancelled_before_its_price_is_known_is_not_unfilled(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fill (any size) with no price yet, whatever the order's final state."""
    lag = Lag(stack.broker, monkeypatch)
    ticket = stack.executor.submit(stack.approved, (), SEND)
    entry = stack.broker.status(ticket.order_id)
    lag.hide(ticket.order_id)
    monkeypatch.setattr(
        stack.broker,
        "status",
        lambda order_id: (
            OrderStatus(entry.order, OrderState.CANCELLED, 1, None)
            if order_id == ticket.order_id
            else SimulatedBroker.status(stack.broker, order_id)
        ),
    )
    ticket = stack.executor.on_time(ticket, SEND + SEC)
    assert ticket.outcome is EntryOutcome.WORKING
    assert ticket.position is None


# --- the exit (Malik) ---------------------------------------------------------------


def _exit_filled_unpriced(stack: Stack, lag: Lag) -> tuple[Position, datetime]:
    """An exit sent and filled at the bid, its price and the new holding withheld."""
    position = filled_position(stack)
    lag.hide(f"{position.position_id}-exit-1")
    lag.hold(stack.broker.positions())  # the report still shows it held
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None
    assert position.exit_order_id == f"{position.position_id}-exit-1"
    assert SimulatedBroker.status(stack.broker, position.exit_order_id).state is (OrderState.FILLED)
    return position, t


def test_an_exit_filled_before_its_price_is_known_is_never_sold_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    for k in range(1, 4):
        stack.quote(150, 151, t + k * SEC)
        assert stack.manager.flatten(position, t + k * SEC) is None
    assert lag.sells() == [f"{position.position_id}-exit-1"]  # never a second sell
    assert position.exit_order_id == f"{position.position_id}-exit-1"  # still followed
    assert not stack.halt.tripped
    lag.reveal()
    closed = stack.manager.flatten(position, t + 5 * SEC)
    assert closed is not None
    assert closed.exit_price == D(160)  # the real fill, once reported


def test_an_exit_whose_price_never_comes_hands_over_to_reconcile_without_selling(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    assert stack.manager.flatten(position, t + _wait(stack) - SEC) is None
    with pytest.raises(NeedsReconcile):
        stack.manager.flatten(position, t + _wait(stack))
    assert lag.sells() == [f"{position.position_id}-exit-1"]


def test_a_resting_exit_that_fills_before_its_price_is_known_is_never_sold_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit rests, then fills while working (Malik's work-exit path)."""
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)
    lag = Lag(stack.broker, monkeypatch)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # rests at 160
    exit_id = position.exit_order_id
    assert exit_id is not None
    lag.hide(exit_id)
    lag.hold(stack.broker.positions())
    stack.quote(160, 161, t + SEC)  # the bid holds at its limit: it fills
    assert SimulatedBroker.status(stack.broker, exit_id).state is OrderState.FILLED
    later = t + 2 * SEC
    stack.quote(150, 151, later)
    assert stack.manager.flatten(position, later) is None
    assert lag.sells() == [exit_id]  # never a second sell
    assert position.exit_order_id == exit_id
    assert not stack.halt.tripped
    lag.reveal()
    closed = stack.manager.flatten(position, later + SEC)
    assert closed is not None
    assert closed.exit_price == D(160)


def test_a_resting_stop_filled_before_its_price_is_known_sends_nothing(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)  # stop 114, limit 109
    assert position.stop_order_id is not None
    lag = Lag(stack.broker, monkeypatch)
    lag.hide(position.stop_order_id)
    lag.hold(stack.broker.positions())
    t = SEND + 30 * SEC
    stack.quote(112, 113, t)  # gaps through the trigger: the broker's stop sells at 112
    assert SimulatedBroker.status(stack.broker, position.stop_order_id).state is (OrderState.FILLED)
    for k in range(3):
        assert stack.step(position, stack.quote(112, 113, t + k * SEC), (), t + k * SEC) is None
    assert lag.sells() == []  # no exit beside the filled stop
    assert not stack.halt.tripped
    lag.reveal()
    closed = stack.step(position, stack.quote(112, 113, t + 4 * SEC), (), t + 4 * SEC)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(112))


def test_a_stop_filled_during_the_exits_cancel_before_its_price_is_known_waits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit cancels the stop; the broker answers that it had already filled."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag = Lag(stack.broker, monkeypatch)
    real_cancel = stack.broker.cancel

    def filled_first(order_id: str) -> OrderStatus:
        if order_id == stop_id:  # it sold at 140 a moment before the cancel arrived
            sim = stack.broker
            held = SimulatedBroker.status(sim, stop_id)
            sim._orders[stop_id] = OrderStatus(held.order, OrderState.FILLED, 1, D(140), True)
            sim._positions.pop(position.contract_id, None)
        return real_cancel(order_id)

    monkeypatch.setattr(stack.broker, "cancel", filled_first)
    lag.hide(stop_id)
    lag.hold({position.contract_id: position.contracts})
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None
    assert lag.sells() == []
    assert not stack.halt.tripped
    lag.reveal()
    closed = stack.manager.flatten(position, t + SEC)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(140))


def _sweep_filled_unpriced(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> tuple[Position, Lag, datetime]:
    """The exit's sweep cancels another sell on the contract; it had filled the whole
    size a moment before (at 141), its price and the new holding withheld."""
    position = filled_position(stack)
    stray = Order("stray-1", position.contract_id, Side.SELL, 1, OrderType.LIMIT, D(300))
    stack.manager._broker.place(stray)
    lag = Lag(stack.broker, monkeypatch)
    real_cancel = stack.broker.cancel

    def filled_first(order_id: str) -> OrderStatus:
        if order_id == "stray-1":
            sim = stack.broker
            sim._orders["stray-1"] = OrderStatus(stray, OrderState.FILLED, 1, D(141))
            sim._positions.pop(position.contract_id, None)
        return real_cancel(order_id)

    monkeypatch.setattr(stack.broker, "cancel", filled_first)
    lag.hide("stray-1")
    lag.hold({position.contract_id: position.contracts})
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # not a reconcile, not a sell
    return position, lag, t


def _flattening(stack: Stack) -> list[str]:
    return [a.message for a in stack.alarms if "flattening" in a.message]


def test_another_sell_filled_in_the_sweep_before_its_price_is_known_waits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position, lag, t = _sweep_filled_unpriced(stack, monkeypatch)
    assert lag.sells() == []
    assert not stack.halt.tripped
    lag.reveal()
    closed = stack.manager.flatten(position, t + SEC)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(141))


def test_while_a_price_is_pending_management_only_waits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The position is sold: neither the management step nor the failsafe's flatten
    starts another exit (no "flattening" alarm, nothing sent) while its price is due."""
    position, lag, t = _sweep_filled_unpriced(stack, monkeypatch)
    before = _flattening(stack)
    assert stack.step(position, stack.quote(150, 151, t + SEC), (), t + SEC) is None
    assert stack.manager.flatten(position, t + 2 * SEC) is None
    assert _flattening(stack) == before
    assert lag.sells() == []


def test_reconcile_books_a_sweep_fill_once_its_price_arrives(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fill that sold the position was not one of its own exits: reconcile still
    books it, at its real price, never by hand."""
    position, lag, t = _sweep_filled_unpriced(stack, monkeypatch)
    lag.reveal()
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    closes = []
    for k in range(1, 5):
        now = t + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.manager.reconcile(position, now)
        closes += stack.manager.take_reconcile_closes()
    assert [c.exit_price for c in closes] == [D(141)]
    assert not [a for a in stack.alarms if "book it with Ledger.book_by_hand" in a.message]


def test_reconcile_never_flattens_what_a_fill_without_a_price_already_sold(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(4):
        now = t + k * settle + k * SEC
        stack.quote(150, 151, now)
        assert not stack.manager.reconcile(position, now)
    assert not [o for o in lag.placed if o.startswith("reconcile-")]  # nothing sold again
    lag.reveal()
    closes = []
    for k in range(4, 8):  # the settle waits run again on the new report
        now = t + k * settle + k * SEC
        stack.quote(150, 151, now)
        stack.manager.reconcile(position, now)
        closes += stack.manager.take_reconcile_closes()
    assert [c.exit_price for c in closes] == [D(160)]
    assert not [o for o in lag.placed if o.startswith("reconcile-")]


def _check_the_fill(stack: Stack) -> list[str]:
    return [a.message for a in stack.alarms if "CHECK THE FILL" in a.message]


def _reconcile_rounds(stack: Stack, position: Position, start: datetime, rounds: int) -> None:
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    for k in range(rounds):
        now = start + k * (settle + SEC)
        stack.quote(150, 151, now)
        assert not stack.manager.reconcile(position, now)


def test_past_the_wait_reconcile_never_sells_beside_a_filled_sell(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3-2 / D40: the holding has had `fill_price_wait_seconds` to catch up and has
    not. The broker still reports the position's own sell FILLED, so a reconcile sell
    could go short: nothing is sold, ever. The kill switch is latched and Andre gets one
    critical CHECK THE FILL alarm telling him to flatten by hand."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    _reconcile_rounds(stack, position, t + _wait(stack), 8)
    assert not [o for o in lag.placed if o.startswith("reconcile-")]
    assert lag.sells() == [f"{position.position_id}-exit-1"]  # the one real sale only
    assert stack.broker.positions() == {position.contract_id: position.contracts}  # lagging
    assert SimulatedBroker.positions(stack.broker) == {}  # never short at the broker
    alarms = _check_the_fill(stack)
    assert len(alarms) == 1
    assert "flatten by hand" in alarms[0]
    assert f"{position.position_id}-exit-1" in alarms[0]
    assert [r for r in stack.halt.reasons if f"{position.position_id}-exit-1 filled" in r]
    assert stack.ledger.account(t + 2 * _wait(stack)).kill_switch_latched


def test_past_the_wait_a_sell_whose_price_came_is_still_never_sold_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sell's price arrives after the wait but the holding still lags: the sale is
    known all the more, so reconcile still sends nothing."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    late = t + _wait(stack)
    _reconcile_rounds(stack, position, late, 2)
    lag.hidden.clear()  # the price is reported; the position report is still held
    _reconcile_rounds(stack, position, late + 10 * SEC, 6)
    assert not [o for o in lag.placed if o.startswith("reconcile-")]
    assert SimulatedBroker.positions(stack.broker) == {}
    assert len(_check_the_fill(stack)) == 1


def test_past_the_wait_the_hold_books_the_real_price_once_the_report_catches_up(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed hold is not a dead end: once the price and the holding arrive,
    reconcile books the sale at its real price (never by hand, never a guess), and
    the latch stays for Andre."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    late = t + _wait(stack)
    _reconcile_rounds(stack, position, late, 3)
    lag.reveal()
    settle = timedelta(seconds=stack.profiles.execution.reconcile.settle_seconds)
    closes = []
    for k in range(3, 8):
        now = late + k * (settle + SEC)
        stack.quote(150, 151, now)
        stack.manager.reconcile(position, now)
        closes += stack.manager.take_reconcile_closes()
    assert [c.exit_price for c in closes] == [D(160)]
    assert not [o for o in lag.placed if o.startswith("reconcile-")]
    assert not [a for a in stack.alarms if "book it with Ledger.book_by_hand" in a.message]
    assert stack.ledger.account(late + 10 * settle).kill_switch_latched


def test_past_the_wait_a_stop_fill_reconcile_found_itself_is_never_sold_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Management never saw it: the resting stop filled with its price withheld and
    reconcile is the first to find the sale. Past the wait still nothing is sold, and
    Andre gets one CHECK THE FILL alarm."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag = Lag(stack.broker, monkeypatch)
    sim = stack.broker
    held = SimulatedBroker.status(sim, stop_id)
    lag.hold(sim.positions())
    sim._orders[stop_id] = OrderStatus(held.order, OrderState.FILLED, 1, D(112))
    sim._positions.pop(position.contract_id, None)
    lag.hide(stop_id)
    t = SEND + 60 * SEC
    _reconcile_rounds(stack, position, t, 3)  # found here: inside the wait
    assert not _check_the_fill(stack)
    _reconcile_rounds(stack, position, t + 3 * SEC + _wait(stack), 6)
    assert not [o for o in lag.placed if o.startswith("reconcile-")]
    assert SimulatedBroker.positions(sim) == {}
    assert len(_check_the_fill(stack)) == 1
    assert stop_id in _check_the_fill(stack)[0]
    assert stack.ledger.account(t + 3 * _wait(stack)).kill_switch_latched


def test_a_stop_whose_price_never_comes_hands_over_to_reconcile(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    position = filled_position(stack)
    assert position.stop_order_id is not None
    lag = Lag(stack.broker, monkeypatch)
    lag.hide(position.stop_order_id)
    lag.hold(stack.broker.positions())
    t = SEND + 30 * SEC
    assert stack.step(position, stack.quote(112, 113, t), (), t) is None  # filled at 112
    late = t + _wait(stack)
    with pytest.raises(NeedsReconcile):
        stack.step(position, stack.quote(112, 113, late), (), late)
    assert lag.sells() == []
    assert lag.cancelled == []  # a filled stop is not asked to cancel: it is the sale


def test_a_stop_filled_during_the_exits_cancel_is_not_a_stuck_cancel(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filled is not "will not cancel": no stuck-cancel alarm, no early hand-over."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id is not None
    lag = Lag(stack.broker, monkeypatch)
    real_cancel = stack.broker.cancel

    def filled_first(order_id: str) -> OrderStatus:
        if order_id == stop_id:
            sim = stack.broker
            held = SimulatedBroker.status(sim, stop_id)
            sim._orders[stop_id] = OrderStatus(held.order, OrderState.FILLED, 1, D(140), True)
            sim._positions.pop(position.contract_id, None)
        return real_cancel(order_id)

    monkeypatch.setattr(stack.broker, "cancel", filled_first)
    lag.hide(stop_id)
    lag.hold({position.contract_id: position.contracts})
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None
    escalate = timedelta(seconds=stack.profiles.execution.reconcile.cancel_escalate_seconds)
    assert escalate < _wait(stack)
    assert stack.manager.flatten(position, t + escalate) is None  # still waiting, no hand-over
    assert not [a for a in stack.alarms if "not cancelled" in a.message]


def test_an_orphan_stop_filled_before_its_price_is_known_is_booked_as_the_stop(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The position lost track of its stop (unprotected path), which had filled."""
    position = filled_position(stack)
    stop_id = position.stop_order_id
    assert stop_id == f"{position.position_id}-stop"
    lag = Lag(stack.broker, monkeypatch)
    lag.hide(stop_id)
    lag.hold(stack.broker.positions())
    t = SEND + 30 * SEC
    stack.quote(112, 113, t)  # the stop fills at 112
    position.stop_order_id = None
    assert stack.manager.flatten(position, t + SEC) is None
    assert lag.sells() == []
    lag.reveal()
    closed = stack.manager.flatten(position, t + 2 * SEC)
    assert closed is not None
    assert (closed.reason, closed.exit_price) == (ExitReason.STOP, D(112))


def test_no_exit_is_ever_placed_beside_one_of_its_own_unpriced_fills(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever path reaches the placing of an exit, an exit of the position already
    filled with its price pending stops it (the last line, independent of the others)."""
    lag = Lag(stack.broker, monkeypatch)
    position, t = _exit_filled_unpriced(stack, lag)
    position.exit_order_id = None  # (as if it were lost track of)
    stack.manager._pending.clear()
    assert stack.manager.flatten(position, t + SEC) is None
    assert lag.sells() == [f"{position.position_id}-exit-1"]


def test_an_exit_that_fills_on_its_reprice_before_its_price_is_known_waits_from_then(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resting exit is re-priced to the bid and fills there at once: the wait for
    its price is counted from that fill."""
    position = filled_position(stack)
    exit_rests_on_arrival(stack, monkeypatch)
    lag = Lag(stack.broker, monkeypatch)
    t = SEND + 60 * SEC
    stack.quote(160, 161, t)
    assert stack.manager.flatten(position, t) is None  # rests at 160
    exit_id = position.exit_order_id
    assert exit_id is not None
    lag.hide(exit_id)
    lag.hold(stack.broker.positions())
    filled_at = t + SEC
    stack.quote(150, 151, filled_at)  # (the quote alone does not reach a 160 limit)
    assert stack.manager.flatten(position, filled_at) is None  # re-priced to 150: fills
    assert SimulatedBroker.status(stack.broker, exit_id).state is OrderState.FILLED
    with pytest.raises(NeedsReconcile):
        stack.manager.flatten(position, filled_at + _wait(stack))
    assert lag.sells() == [exit_id]
