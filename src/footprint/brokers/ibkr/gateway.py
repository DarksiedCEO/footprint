"""The narrow seam between the adapter and IBKR's API library.

`IbGateway` is everything the adapter asks of IBKR; the binding to the official
TWS API (EClient) implements it in a few lines per call. Everything IBKR tells us
arrives as calls on `IbEvents` (the adapter implements it; the binding's EWrapper
callbacks forward to it). Keeping the seam this narrow means the adapter's logic,
which is where the risk lives, is tested in full against a fake gateway.

Status strings and error codes are IBKR's, from its documentation (TWS API message
codes): 103 duplicate order id, 104 cannot modify a filled order, 105 modified order
does not match the original, 110 price precision, 135 order not found, 161 cannot
cancel in the current state, 201 rejected, 202 cancelled, 1100 connectivity lost,
1101 restored (data lost), 1102 restored (data maintained).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IbContract:
    """An option contract as IBKR knows it (resolved from its contract details)."""

    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    expiry: str  # YYYYMMDD
    strike: Decimal
    right: str  # "C" or "P"
    multiplier: str
    trading_class: str


@dataclass(frozen=True, slots=True)
class IbOrderRequest:
    ib_id: int
    contract: IbContract
    action: str  # "BUY" or "SELL"
    quantity: int
    order_type: str  # "LMT" or "STP LMT"
    limit_price: Decimal
    stop_price: Decimal | None
    order_ref: str  # our order id: survives restarts, so orders can be matched back
    oca_group: str | None
    oca_type: int | None  # 1 = cancel the rest of the group, with block
    tif: str  # "DAY"


class Connection(Enum):
    CONNECTED = "CONNECTED"  # a fresh session (next valid order id known)
    LOST = "LOST"  # socket closed, or 1100
    RESTORED_DATA_LOST = "RESTORED_DATA_LOST"  # 1101
    RESTORED = "RESTORED"  # 1102


class IbGateway(Protocol):
    def place_order(self, request: IbOrderRequest) -> None: ...

    def cancel_order(self, ib_id: int) -> None: ...

    def global_cancel(self) -> None: ...

    def request_open_orders(self) -> None:
        """All open orders of every client (reqAllOpenOrders)."""

    def request_executions(self) -> None: ...

    def request_positions(self) -> None:
        """Subscribe to positions: a snapshot, positions_end, then every change."""


class IbEvents(Protocol):
    def on_connection(self, state: Connection, next_valid_id: int | None = None) -> None: ...

    def on_open_order(
        self,
        ib_id: int,
        perm_id: int,
        order_ref: str,
        con_id: int,
        symbol: str,
        sec_type: str,
        action: str,
        quantity: int,
        order_type: str,
        limit_price: Decimal,
        stop_price: Decimal | None,
        oca_group: str | None,
        status: str,
    ) -> None: ...

    def on_open_orders_end(self) -> None: ...

    def on_order_status(
        self,
        ib_id: int,
        perm_id: int,
        status: str,
        filled: int,
        remaining: int,
        avg_fill_price: Decimal,
    ) -> None: ...

    def on_execution(
        self,
        exec_id: str,
        ib_id: int,
        perm_id: int,
        con_id: int,
        symbol: str,
        sec_type: str,
        side: str,  # "BOT" or "SLD"
        shares: int,
        price: Decimal,
    ) -> None: ...

    def on_executions_end(self) -> None: ...

    def on_position(self, con_id: int, symbol: str, sec_type: str, position: int) -> None: ...

    def on_positions_end(self) -> None: ...

    def on_error(self, req_id: int, code: int, message: str) -> None: ...
