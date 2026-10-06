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
1101 restored (data lost), 1102 restored (data maintained). Warnings that are not an
order's answer: 165, 399, 10167, 10349 and the 2100-2199 notices.

The binding passes order prices as exact decimals (Decimal(str(price))), so the
adapter can compare IBKR's echo of a modify with what it asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        """Subscribe to positions: a snapshot, positions_end, then every change. Asked
        again (a snapshot can be retaken), the binding cancels the subscription
        (cancelPositions) and subscribes anew."""


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


# --- market data -----------------------------------------------------------------------
#
# Separate from the order seam: the market data service asks for contracts, option
# chains, quotes and bars, under request ids it chooses (from MARKET_REQUEST_BASE up,
# far above any order id, so an error's request id is never ambiguous).

# Half of IBKR's signed 32-bit request id range (a power of two, written out in full).
MARKET_REQUEST_BASE = 1073741824


@dataclass(frozen=True, slots=True)
class ContractQuery:
    """What to look up (reqContractDetails), or a contract to stream or chart by fields.
    Empty strings and None are 'any'."""

    symbol: str
    sec_type: str  # "FUT", "FOP", "CONTFUT"
    exchange: str
    currency: str
    expiry: str = ""  # YYYYMM or YYYYMMDD
    strike: Decimal | None = None
    right: str = ""  # "C" or "P"
    trading_class: str = ""
    con_id: int = 0


@dataclass(frozen=True, slots=True)
class IbBar:
    """One completed (or, in an update, still forming) 1-minute bar."""

    ts: datetime  # when the bar opens, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class QuoteField(Enum):
    BID = "BID"
    ASK = "ASK"
    OPEN_INTEREST = "OPEN_INTEREST"


class IbMarketGateway(Protocol):
    def request_contract_details(self, req_id: int, query: ContractQuery) -> None: ...

    def request_option_params(
        self, req_id: int, symbol: str, underlying_sec_type: str, underlying_con_id: int
    ) -> None:
        """The option chains (expirations, strikes) on an underlying (reqSecDefOptParams)."""

    def request_quotes(self, req_id: int, contract: IbContract) -> None:
        """Stream real-time top of book (and open interest for an option). Delayed data
        is never asked for, and never accepted."""

    def cancel_quotes(self, req_id: int) -> None: ...

    def request_bars(
        self, req_id: int, query: ContractQuery, end: str, duration: str, keep_up_to_date: bool
    ) -> None:
        """1-minute TRADES bars, all hours, ending at `end` (IBKR's format; "" = now).
        With keep_up_to_date the forming bar keeps arriving as updates."""

    def cancel_bars(self, req_id: int) -> None: ...


class IbMarketEvents(Protocol):
    def on_contract_details(self, req_id: int, contract: IbContract) -> None: ...

    def on_contract_details_end(self, req_id: int) -> None: ...

    def on_option_params(
        self,
        req_id: int,
        exchange: str,
        trading_class: str,
        multiplier: str,
        expirations: frozenset[str],
        strikes: frozenset[Decimal],
    ) -> None: ...

    def on_option_params_end(self, req_id: int) -> None: ...

    def on_quote(self, req_id: int, field: QuoteField, value: Decimal | None) -> None:
        """None: IBKR says there is no such price now (no bid, no ask)."""

    def on_bar(self, req_id: int, bar: IbBar) -> None: ...

    def on_bars_end(self, req_id: int) -> None: ...

    def on_bar_update(self, req_id: int, bar: IbBar) -> None: ...

    def on_market_error(self, req_id: int, code: int, message: str) -> None: ...
