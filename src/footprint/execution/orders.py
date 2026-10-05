"""Orders and the order-capable broker seam (floor 4).

Limit orders only on options (NB-12): the only order types that exist are LIMIT
and STOP_LIMIT. There is no market order type to send, by construction.

`SimulatedBroker` is the backtest/paper stand-in, conservative on purpose:
- an order that is marketable when it arrives (placed, modified, or a stop-limit
  the moment it triggers) fills at the touch: the ask for a buy, the bid for a sell;
- a resting order that a later quote crosses fills at its LIMIT price, never better;
- a buy needs the ask at or below its limit, a sell needs the bid at or above it,
  and a zero bid or ask never fills anything.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from footprint.core.models import require_utc

log = logging.getLogger("footprint.orders")


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    LIMIT = "LIMIT"
    STOP_LIMIT = "STOP_LIMIT"


class OrderState(Enum):
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    contract_id: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal
    stop_price: Decimal | None = None
    # One-cancels-all: orders sharing a group can never both fill. When one fills the
    # broker cancels the rest, and holds them back while one is routed ("with block").
    # A position's stop and exits share one group (its position id).
    oca_group: str | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "contract_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Order.{name} must be a non-empty string")
        if not isinstance(self.side, Side):
            raise TypeError("Order.side must be a Side")
        if not isinstance(self.order_type, OrderType):
            raise TypeError("Order.order_type must be an OrderType (LIMIT or STOP_LIMIT)")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise TypeError("Order.quantity must be an int")
        if self.quantity < 1:
            raise ValueError("Order.quantity must be >= 1")
        _price(self.limit_price, "Order.limit_price")
        if self.order_type is OrderType.STOP_LIMIT:
            if self.stop_price is None:
                raise ValueError("a STOP_LIMIT order needs a stop_price")
            _price(self.stop_price, "Order.stop_price")
            if self.side is Side.SELL and self.limit_price > self.stop_price:
                raise ValueError("a sell stop-limit's limit must be at or below its stop")
        elif self.stop_price is not None:
            raise ValueError("only a STOP_LIMIT order has a stop_price")


def _price(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a finite Decimal > 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class OrderStatus:
    order: Order
    state: OrderState
    filled: int
    avg_price: Decimal | None
    triggered: bool = False


class DuplicateOrderId(ValueError):
    """The broker already has an order with this id. A broker adapter must raise this
    (and only this) for a duplicate id: re-sending an order under its own id is how a
    lost answer is resolved safely (see TrackedBroker.place)."""


class PositionsUncertain(ConnectionError):
    """The broker cannot say right now what is held (its fills and its position
    report disagree). Nothing may be sold or booked on it: wait and ask again."""


@runtime_checkable
class OrderBroker(Protocol):
    """What execution needs from a broker. The broker is the source of truth for
    positions; the system's own records are reconciled against it.
    """

    def place(self, order: Order) -> OrderStatus: ...

    def modify(
        self, order_id: str, limit_price: Decimal, stop_price: Decimal | None
    ) -> OrderStatus: ...

    def cancel(self, order_id: str) -> OrderStatus: ...

    def status(self, order_id: str) -> OrderStatus: ...

    def positions(self) -> dict[str, int]: ...

    def open_orders(self) -> list[Order]: ...


class TrackedBroker:
    """The one record of every order the system sends, wrapped around the broker.

    An order is recorded BEFORE it is sent (so a send whose answer is lost is still
    known) and forgotten only when `status` confirms it finished. `working()` is the
    set every sweep reads: the broker's list, plus every recorded order not confirmed
    finished. An order whose status cannot be read counts as working for its full
    size (it errs toward selling too little, never too much). A live broker can list
    a new order late; this record never depends on the list alone.
    """

    def __init__(self, inner: OrderBroker) -> None:
        self._inner = inner
        self._sent: dict[str, Order] = {}

    @property
    def inner(self) -> OrderBroker:
        return self._inner

    @property
    def unfinished(self) -> dict[str, Order]:
        """Recorded orders not yet confirmed finished (a copy)."""
        return dict(self._sent)

    def place(self, order: Order) -> OrderStatus:
        """Record, then send. If the send fails, the order may or may not have reached
        the broker; it is sent once more under the SAME id, which is safe either way:
        the broker takes it (the first never arrived) or refuses the duplicate id (the
        first did). Only if that second send also fails is the outcome unknown; the
        error is raised, and the order stays recorded as possibly working."""
        self._sent[order.order_id] = order  # recorded before it can reach the broker
        try:
            return self._seen(self._inner.place(order))
        except DuplicateOrderId:
            raise  # the caller reused an id: a bug, never a lost answer
        except Exception as first:
            log.critical(
                "send of %s failed (%r); sending again under its id", order.order_id, first
            )
            try:
                return self._seen(self._inner.place(order))
            except DuplicateOrderId:
                log.critical("%s had reached the broker after all", order.order_id)
                return self.status_or_working(order.order_id, order)
            except Exception:
                raise first from None

    def modify(
        self, order_id: str, limit_price: Decimal, stop_price: Decimal | None
    ) -> OrderStatus:
        return self._seen(self._inner.modify(order_id, limit_price, stop_price))

    def cancel(self, order_id: str) -> OrderStatus:
        return self._seen(self._inner.cancel(order_id))

    def status(self, order_id: str) -> OrderStatus:
        return self._seen(self._inner.status(order_id))

    def _seen(self, status: OrderStatus) -> OrderStatus:
        """Any answer from the broker that an order is finished ends its record."""
        if status.state is not OrderState.WORKING:
            self._sent.pop(status.order.order_id, None)
        return status

    def positions(self) -> dict[str, int]:
        return self._inner.positions()

    def open_orders(self) -> list[Order]:
        return self._inner.open_orders()

    def working(self) -> dict[str, OrderStatus]:
        """Every order that may still be working, by id (see the class docstring)."""
        known: dict[str, Order] = {o.order_id: o for o in self._inner.open_orders()}
        for order_id, order in self._sent.items():
            known.setdefault(order_id, order)
        working: dict[str, OrderStatus] = {}
        for order_id, order in known.items():
            status = self.status_or_working(order_id, order)
            if status.state is OrderState.WORKING:
                working[order_id] = status
        return working

    def status_or_working(self, order_id: str, order: Order) -> OrderStatus:
        """The broker's status, or (if it cannot be read) `order` as wholly working."""
        try:
            return self.status(order_id)
        except Exception as exc:
            log.critical("cannot read the status of %s: %r", order_id, exc)
            return OrderStatus(order, OrderState.WORKING, 0, None)


@dataclass(frozen=True, slots=True)
class Quote:
    contract_id: str
    bid: Decimal
    ask: Decimal
    ts: datetime

    def __post_init__(self) -> None:
        require_utc(self.ts, "Quote.ts")
        for name in ("bid", "ask"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"Quote.{name} must be a finite Decimal >= 0")
        if self.ask < self.bid:
            raise ValueError("Quote is crossed")


class QuoteSource(Protocol):
    def subscribe(self, listener: Callable[[Quote], None]) -> None: ...

    def latest(self, contract_id: str) -> Quote | None: ...

    @property
    def newest(self) -> datetime | None: ...


class SimulatedBroker:
    """A pessimistic in-memory broker for backtests, paper rehearsal and tests."""

    def __init__(self, book: QuoteSource, max_quote_age_seconds: int) -> None:
        """Fills only against `book`, the same quotes the rest of the system reads, and
        never against a quote older than `max_quote_age_seconds` behind the book's
        newest quote."""
        self._max_age = timedelta(seconds=max_quote_age_seconds)
        self._orders: dict[str, OrderStatus] = {}
        self._positions: dict[str, int] = {}
        self._book = book
        book.subscribe(self._on_quote)
        self.log: list[str] = []

    # --- market --------------------------------------------------------------------

    def _on_quote(self, quote: Quote) -> None:
        for order_id in list(self._orders):
            status = self._orders[order_id]  # re-read: a group fill may have cancelled it
            if status.state is OrderState.WORKING and status.order.contract_id == quote.contract_id:
                self._store(order_id, self._try_fill(status, quote, at_touch=False))

    def _store(self, order_id: str, status: OrderStatus) -> None:
        """Record a status; a fill cancels the rest of its one-cancels-all group."""
        self._orders[order_id] = status
        group = status.order.oca_group
        if status.state is not OrderState.FILLED or group is None:
            return
        for other_id, other in list(self._orders.items()):
            if (
                other_id != order_id
                and other.order.oca_group == group
                and other.state is OrderState.WORKING
            ):
                self._orders[other_id] = replace(other, state=OrderState.CANCELLED)
                self.log.append(f"oca-cancelled {other_id} (group {group} filled)")

    def _group_filled(self, group: str | None) -> bool:
        return group is not None and any(
            s.order.oca_group == group and s.state is OrderState.FILLED
            for s in self._orders.values()
        )

    def _try_fill(self, status: OrderStatus, quote: Quote, *, at_touch: bool) -> OrderStatus:
        order = status.order
        newest = self._book.newest
        if newest is not None and newest - quote.ts > self._max_age:
            return status  # a stale quote fills nothing
        triggered = status.triggered
        if order.order_type is OrderType.STOP_LIMIT and not triggered:
            assert order.stop_price is not None
            if quote.bid <= order.stop_price:
                triggered = True
                at_touch = True  # it becomes a live sell limit right now
                self.log.append(f"triggered {order.order_id} at bid {quote.bid}")
            else:
                return status
        crosses = (
            quote.ask > 0 and quote.ask <= order.limit_price
            if order.side is Side.BUY
            else quote.bid > 0 and quote.bid >= order.limit_price
        )
        if not crosses:
            return replace(status, triggered=triggered)
        qty = order.quantity - status.filled
        signed = qty if order.side is Side.BUY else -qty
        self._positions[order.contract_id] = self._positions.get(order.contract_id, 0) + signed
        if self._positions[order.contract_id] == 0:
            del self._positions[order.contract_id]
        touch = quote.ask if order.side is Side.BUY else quote.bid
        price = touch if at_touch else order.limit_price
        self.log.append(f"filled {order.order_id} {order.side.value} {qty} @ {price}")
        return OrderStatus(order, OrderState.FILLED, order.quantity, price, triggered)

    # --- OrderBroker ---------------------------------------------------------------

    def place(self, order: Order) -> OrderStatus:
        if order.order_id in self._orders:
            raise DuplicateOrderId(f"duplicate order id {order.order_id!r}")
        status = OrderStatus(order, OrderState.WORKING, 0, None)
        self.log.append(f"placed {order.order_id} {order.order_type.value} {order.side.value}")
        if self._group_filled(order.oca_group):
            # A member of its one-cancels-all group already filled: it is cancelled.
            status = replace(status, state=OrderState.CANCELLED)
            self.log.append(f"oca-cancelled {order.order_id} (group {order.oca_group} filled)")
            self._orders[order.order_id] = status
            return status
        quote = self._book.latest(order.contract_id)
        if quote is not None:
            status = self._try_fill(status, quote, at_touch=True)
        self._store(order.order_id, status)
        return status

    def modify(
        self, order_id: str, limit_price: Decimal, stop_price: Decimal | None
    ) -> OrderStatus:
        status = self._orders[order_id]
        if status.state is not OrderState.WORKING:
            return status
        order = replace(status.order, limit_price=limit_price, stop_price=stop_price)
        status = replace(status, order=order)
        quote = self._book.latest(order.contract_id)
        if quote is not None:
            status = self._try_fill(status, quote, at_touch=True)
        self._store(order_id, status)
        self.log.append(f"modified {order_id} limit {limit_price} stop {stop_price}")
        return status

    def cancel(self, order_id: str) -> OrderStatus:
        status = self._orders[order_id]
        if status.state is OrderState.WORKING:
            status = replace(status, state=OrderState.CANCELLED)
            self._orders[order_id] = status
            self.log.append(f"cancelled {order_id}")
        return status

    def status(self, order_id: str) -> OrderStatus:
        return self._orders[order_id]

    def positions(self) -> dict[str, int]:
        return dict(self._positions)

    def open_orders(self) -> list[Order]:
        return [s.order for s in self._orders.values() if s.state is OrderState.WORKING]

    working_orders = open_orders
