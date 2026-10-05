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

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from footprint.core.models import require_utc


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


class SimulatedBroker:
    """A pessimistic in-memory broker for backtests, paper rehearsal and tests."""

    def __init__(self) -> None:
        self._orders: dict[str, OrderStatus] = {}
        self._positions: dict[str, int] = {}
        self._quotes: dict[str, Quote] = {}
        self.log: list[str] = []

    # --- market --------------------------------------------------------------------

    def on_quote(self, quote: Quote) -> None:
        self._quotes[quote.contract_id] = quote
        for order_id, status in list(self._orders.items()):
            if status.state is OrderState.WORKING and status.order.contract_id == quote.contract_id:
                self._orders[order_id] = self._try_fill(status, quote, at_touch=False)

    def _try_fill(self, status: OrderStatus, quote: Quote, *, at_touch: bool) -> OrderStatus:
        order = status.order
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
            raise ValueError(f"duplicate order id {order.order_id!r}")
        status = OrderStatus(order, OrderState.WORKING, 0, None)
        self.log.append(f"placed {order.order_id} {order.order_type.value} {order.side.value}")
        quote = self._quotes.get(order.contract_id)
        if quote is not None:
            status = self._try_fill(status, quote, at_touch=True)
        self._orders[order.order_id] = status
        return status

    def modify(
        self, order_id: str, limit_price: Decimal, stop_price: Decimal | None
    ) -> OrderStatus:
        status = self._orders[order_id]
        if status.state is not OrderState.WORKING:
            return status
        order = replace(status.order, limit_price=limit_price, stop_price=stop_price)
        status = replace(status, order=order)
        quote = self._quotes.get(order.contract_id)
        if quote is not None:
            status = self._try_fill(status, quote, at_touch=True)
        self._orders[order_id] = status
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
