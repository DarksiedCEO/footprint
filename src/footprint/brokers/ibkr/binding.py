"""The binding: IbGateway over IBKR's official API (ibapi, pinned by
scripts/install_ibapi.py), and IBKR's callbacks forwarded to IbEvents.

This layer only translates. Every decision about what to trust lives in the adapter
(IbkrBroker), which is tested against a fake IBKR. What the binding adds:

- One account. Orders are sent for `account` only; orders, executions and positions
  of any other account on the same login (or with no account at all) never reach the
  adapter, and a broker-wide cancel is refused while any such order is open (it
  would cancel them too).
- One instrument read exactly. Fills and positions outside the system's scope are
  dropped before any number in them is read, and an order outside it is passed on
  only so the adapter knows it is open: a fractional stock holding of Andre's can
  never upset the system.
- Paper only (while `require_paper` is set): the connection is refused, and never
  retried, unless the login manages `account` and that account is a paper account.
  The adapter hears CONNECTED only after that check, with IBKR's next valid order id.
- Exact numbers: prices arrive as floats and become Decimals through their shortest
  text form (151.25 -> Decimal("151.25")); quantities must be whole numbers. A value
  that cannot be read exactly is never guessed: the connection is dropped and alarmed,
  and the reconnect's resync reads the account afresh.
- Stops trigger outside regular trading hours too (outsideRth), since the system
  holds positions in the overnight session.
- A supervisor (`run`) reconnects after any drop, after `reconnect_seconds`.

Callbacks arrive on ibapi's message thread. The adapter's waits happen on the
caller's thread, never on this one.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData, TickAttrib
from ibapi.const import UNSET_DECIMAL, UNSET_DOUBLE
from ibapi.contract import Contract, ContractDetails
from ibapi.execution import Execution, ExecutionFilter
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.order_state import OrderState
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from footprint.brokers.ibkr.gateway import (
    MARKET_DATA_NOT_REAL_TIME,
    MARKET_DATA_UNREADABLE,
    MARKET_REQUEST_BASE,
    Connection,
    ContractQuery,
    IbBar,
    IbContract,
    IbEvents,
    IbMarketEvents,
    IbOrderRequest,
    QuoteField,
)
from footprint.core.config import IbkrConfig

log = logging.getLogger("footprint.ibkr.binding")

_STOP_TYPES = frozenset({"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"})
# Inactive is not finished: an inactive order can become active again.
_FINISHED = frozenset({"Filled", "Cancelled", "ApiCancelled"})
_REAL_TIME = 1  # reqMarketDataType: live streaming only (3 would be delayed)
_OPEN_INTEREST_TICKS = "101"  # generic tick list: option open interest
_BAR_SIZE = "1 min"
_BARS_OF = "TRADES"
_ALL_HOURS = 0  # useRTH
_EPOCH_DATES = 2  # formatDate: seconds since the epoch, UTC
_QUOTE_PRICES = {TickTypeEnum.BID: QuoteField.BID, TickTypeEnum.ASK: QuoteField.ASK}
_OPEN_INTEREST = frozenset(
    {TickTypeEnum.OPTION_CALL_OPEN_INTEREST, TickTypeEnum.OPTION_PUT_OPEN_INTEREST}
)


class ForeignOrdersOpen(PermissionError):
    """A broker-wide cancel would reach orders of another account on the login."""


class UnreadableValue(ValueError):
    """IBKR sent a number that cannot be read exactly."""


@dataclass(frozen=True, slots=True)
class IbConnectionSettings:
    host: str
    port: int
    client_id: int
    account: str
    reconnect_seconds: float
    require_paper: bool
    paper_account_prefix: str

    @classmethod
    def from_env(cls, env: dict[str, str], ibkr: IbkrConfig) -> IbConnectionSettings:
        """From BROKER_HOST, BROKER_PORT, BROKER_ACCOUNT_ID, BROKER_CLIENT_ID."""
        missing = [
            k
            for k in ("BROKER_HOST", "BROKER_PORT", "BROKER_ACCOUNT_ID", "BROKER_CLIENT_ID")
            if not env.get(k, "").strip()
        ]
        if missing:
            raise ValueError(f"missing in .env: {', '.join(missing)}")
        try:
            port, client_id = int(env["BROKER_PORT"]), int(env["BROKER_CLIENT_ID"])
        except ValueError as exc:
            raise ValueError("BROKER_PORT and BROKER_CLIENT_ID must be whole numbers") from exc
        return cls(
            host=env["BROKER_HOST"].strip(),
            port=port,
            client_id=client_id,
            account=env["BROKER_ACCOUNT_ID"].strip(),
            reconnect_seconds=float(ibkr.reconnect_seconds),
            require_paper=ibkr.require_paper,
            paper_account_prefix=ibkr.paper_account_prefix,
        )


def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored. No shell expansion."""
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            key, sep, value = text.partition("=")
            if not sep or not key.strip():
                raise ValueError(f"{path}:{number}: expected KEY=VALUE")
            env[key.strip()] = value.strip()
    return env


def to_price(value: float) -> Decimal | None:
    """IBKR's float as an exact Decimal; None when IBKR left it unset."""
    if value == UNSET_DOUBLE or math.isnan(value) or math.isinf(value):
        return None
    return Decimal(repr(float(value)))


def to_quantity(value: Decimal | float | int) -> int:
    """A whole number of contracts, or UnreadableValue."""
    if isinstance(value, Decimal) and value == UNSET_DECIMAL:
        raise UnreadableValue("quantity left unset by IBKR")
    exact = value if isinstance(value, Decimal) else Decimal(repr(value))
    if not exact.is_finite() or exact != exact.to_integral_value():
        raise UnreadableValue(f"quantity {value!r} is not a whole number")
    return int(exact)


def build_query(query: ContractQuery) -> Contract:
    contract = Contract()
    contract.conId = query.con_id
    contract.symbol = query.symbol
    contract.secType = query.sec_type
    contract.exchange = query.exchange
    contract.currency = query.currency
    contract.lastTradeDateOrContractMonth = query.expiry
    if query.strike is not None:
        contract.strike = float(query.strike)
    contract.right = query.right
    contract.tradingClass = query.trading_class
    return contract


def build_ib_contract(c: IbContract) -> Contract:
    contract = Contract()
    contract.conId = c.con_id
    contract.symbol = c.symbol
    contract.secType = c.sec_type
    contract.exchange = c.exchange
    contract.currency = c.currency
    contract.lastTradeDateOrContractMonth = c.expiry
    contract.strike = float(c.strike)
    contract.right = c.right
    contract.multiplier = c.multiplier
    contract.tradingClass = c.trading_class
    return contract


def from_details(details: ContractDetails) -> IbContract:
    c = details.contract
    strike = to_price(c.strike)
    return IbContract(
        con_id=c.conId,
        symbol=c.symbol,
        sec_type=c.secType,
        exchange=c.exchange,
        currency=c.currency,
        expiry=c.lastTradeDateOrContractMonth,
        strike=strike if strike is not None else Decimal(0),
        right=c.right,
        multiplier=c.multiplier,
        trading_class=c.tradingClass,
    )


def to_bar(bar: BarData) -> IbBar:
    """A 1-minute bar asked for with epoch dates; anything else is unreadable."""
    if not str(bar.date).isdigit():
        raise UnreadableValue(f"bar date {bar.date!r} is not in seconds since the epoch")
    prices = [to_price(p) for p in (bar.open, bar.high, bar.low, bar.close)]
    if any(p is None or p <= 0 for p in prices):
        raise UnreadableValue(f"bar at {bar.date} has an unreadable price")
    o, h, low, c = (p for p in prices if p is not None)
    if not (low <= o <= h and low <= c <= h):
        raise UnreadableValue(f"bar at {bar.date} is not a bar: {o} {h} {low} {c}")
    volume = to_quantity(bar.volume)
    if volume < 0:
        raise UnreadableValue(f"bar at {bar.date} has volume {volume}")
    try:
        ts = datetime.fromtimestamp(int(bar.date), tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise UnreadableValue(f"bar date {bar.date!r} is out of range") from exc
    return IbBar(ts, o, h, low, c, volume)


def build_contract(request: IbOrderRequest) -> Contract:
    c = request.contract
    contract = Contract()
    contract.conId = c.con_id
    contract.symbol = c.symbol
    contract.secType = c.sec_type
    contract.exchange = c.exchange
    contract.currency = c.currency
    contract.lastTradeDateOrContractMonth = c.expiry
    contract.strike = float(c.strike)
    contract.right = c.right
    contract.multiplier = c.multiplier
    contract.tradingClass = c.trading_class
    return contract


def build_order(request: IbOrderRequest, account: str) -> Order:
    order = Order()
    order.action = request.action
    order.totalQuantity = Decimal(request.quantity)
    order.orderType = request.order_type
    order.lmtPrice = float(request.limit_price)
    if request.stop_price is not None:
        order.auxPrice = float(request.stop_price)
    order.tif = request.tif
    order.orderRef = request.order_ref
    if request.oca_group is not None:
        order.ocaGroup = request.oca_group
        if request.oca_type is not None:
            order.ocaType = request.oca_type
    order.account = account
    order.transmit = True
    order.outsideRth = True
    return order


class IbBinding:
    """IbGateway over ibapi. `attach` the adapter (IbEvents) before `run`."""

    def __init__(
        self,
        settings: IbConnectionSettings,
        scope: tuple[str, str],
        on_alarm: Callable[[str], None] = lambda message: None,
        client_factory: Callable[[EWrapper], Any] | None = None,
    ) -> None:
        self._s = settings
        self._scope = scope
        self._on_alarm = on_alarm
        self._factory: Callable[[EWrapper], Any] = client_factory or EClient
        self._events: IbEvents | None = None
        self._market: IbMarketEvents | None = None
        self._lock = threading.Lock()
        self._client: Any = None
        self._next_request = 1
        self._sent_ids: set[int] = set()  # IBKR order ids this binding has sent
        self._refused: str | None = None
        self._wrapper = _Wrapper(self)

    @property
    def refused(self) -> str | None:
        """Why the connection was refused for good (wrong or live account), if it was."""
        return self._refused

    def attach(self, events: IbEvents) -> None:
        self._events = events

    def attach_market(self, events: IbMarketEvents) -> None:
        """The market data service, if one runs (the read-only check has none). Attach
        it before run(): market callbacks with nothing attached are dropped."""
        self._market = events

    # --- IbMarketGateway ----------------------------------------------------------------

    def request_contract_details(self, req_id: int, query: ContractQuery) -> None:
        self._wrapper.claim(req_id)
        self._call().reqContractDetails(req_id, build_query(query))

    def request_option_params(
        self,
        req_id: int,
        symbol: str,
        option_exchange: str,
        underlying_sec_type: str,
        underlying_con_id: int,
    ) -> None:
        self._wrapper.claim(req_id)
        self._call().reqSecDefOptParams(
            req_id, symbol, option_exchange, underlying_sec_type, underlying_con_id
        )

    def request_quotes(self, req_id: int, contract: IbContract) -> None:
        self._wrapper.claim(req_id)
        self._call().reqMktData(
            req_id, build_ib_contract(contract), _OPEN_INTEREST_TICKS, False, False, []
        )

    def cancel_quotes(self, req_id: int) -> None:
        self._call().cancelMktData(req_id)

    def request_bars(
        self, req_id: int, query: ContractQuery, end: str, duration: str, keep_up_to_date: bool
    ) -> None:
        if keep_up_to_date and end:
            raise ValueError(f"bars kept up to date must end now (''), not {end!r}")
        self._wrapper.claim(req_id)
        self._call().reqHistoricalData(
            req_id,
            build_query(query),
            end,
            duration,
            _BAR_SIZE,
            _BARS_OF,
            _ALL_HOURS,
            _EPOCH_DATES,
            keep_up_to_date,
            [],
        )

    def cancel_bars(self, req_id: int) -> None:
        self._call().cancelHistoricalData(req_id)

    # --- IbGateway --------------------------------------------------------------------

    def place_order(self, request: IbOrderRequest) -> None:
        with self._lock:
            self._sent_ids.add(request.ib_id)
        self._call().placeOrder(
            request.ib_id, build_contract(request), build_order(request, self._s.account)
        )

    def cancel_order(self, ib_id: int) -> None:
        self._call().cancelOrder(ib_id, OrderCancel())

    def global_cancel(self) -> None:
        others = self._wrapper.other_accounts()
        if others:
            # Orders placed in those accounts since the last listing may be unseen.
            raise ForeignOrdersOpen(
                f"this login also manages {list(others)}: a broker-wide cancel would "
                "reach their orders: cancel by hand"
            )
        foreign = self._wrapper.foreign_open()
        if foreign:
            raise ForeignOrdersOpen(
                f"a broker-wide cancel would also cancel {foreign} order(s) of other "
                "accounts on this login: cancel by hand"
            )
        self._call().reqGlobalCancel(OrderCancel())

    def request_open_orders(self) -> None:
        self._call().reqAllOpenOrders()

    def request_executions(self) -> None:
        with self._lock:
            req_id = self._next_request
            self._next_request += 1
        execution_filter = ExecutionFilter()
        execution_filter.acctCode = self._s.account
        self._call().reqExecutions(req_id, execution_filter)

    def request_positions(self) -> None:
        client = self._call()
        client.cancelPositions()  # a retaken snapshot starts a fresh subscription
        client.reqPositions()

    # --- the connection ------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Connect, process IBKR's messages, and reconnect after any drop, until
        `stop` is set or the account is refused."""
        if self._refused is not None:
            return  # refused for good: never connect again
        while not stop.is_set():
            client = self._factory(self._wrapper)
            with self._lock:
                self._client = client
            self._wrapper.new_session()
            log.info("connecting to IB Gateway at %s:%s", self._s.host, self._s.port)
            try:
                client.connect(self._s.host, self._s.port, self._s.client_id)
                if client.isConnected():
                    client.run()  # until the connection drops (it disconnects on the way out)
            except Exception as exc:  # a bug anywhere must never end the reconnect loop
                log.exception("IBKR session failed")
                self._alarm(f"IBKR session failed unexpectedly ({exc!r}); reconnecting")
                self._disconnect(client)
            try:
                self._wrapper.closed()
            except Exception as exc:
                log.exception("IBKR session close failed")
                self._alarm(f"IBKR session close failed ({exc!r}); reconnecting anyway")
            if self._refused is not None:
                return
            stop.wait(self._s.reconnect_seconds)  # returns at once when stopped

    def stop(self) -> None:
        """Disconnect now (run returns once `stop` is also set)."""
        with self._lock:
            client = self._client
        if client is not None:
            client.disconnect()

    @staticmethod
    def _disconnect(client: Any) -> None:
        try:
            client.disconnect()
        except Exception:
            log.exception("IBKR disconnect failed")

    def _sent(self, ib_id: int) -> bool:
        with self._lock:
            return ib_id in self._sent_ids

    def _call(self) -> Any:
        with self._lock:
            client = self._client
        if client is None:
            raise ConnectionError("IB Gateway is not connected")
        return client

    # --- used by the wrapper ---------------------------------------------------------

    def _market_events(self) -> IbMarketEvents | None:
        return self._market

    def _forward(self) -> IbEvents:
        if self._events is None:
            raise RuntimeError("no adapter attached to the IBKR binding")
        return self._events

    def _refuse(self, reason: str) -> None:
        self._refused = reason
        self._alarm(f"IBKR CONNECTION REFUSED: {reason}")
        with self._lock:
            client = self._client
        if client is not None:
            client.disconnect()

    def _alarm(self, message: str) -> None:
        log.critical(message)
        try:
            self._on_alarm(message)
        except Exception:
            log.exception("could not deliver an IBKR binding alarm")


class _Wrapper(EWrapper):  # type: ignore[misc]
    """IBKR's callbacks, translated. An unreadable order or position value drops the
    connection; an unreadable market value voids only its own request."""

    def __init__(self, binding: IbBinding) -> None:
        super().__init__()
        self._b = binding
        self._session_lock = threading.Lock()
        self.new_session()

    def new_session(self) -> None:
        with self._session_lock:
            self._next_id: int | None = None
            self._accounts_ok = False
            self._announced = False
            self._open = False
            self._foreign: set[int] = set()  # permIds of other accounts' orders
            self._foreign_live: set[int] = set()  # ...still open
            self._outside: set[int] = set()  # permIds of orders outside the scope
            self._in_scope: set[int] = set()  # permIds of orders in the scope
            self._accounts: tuple[str, ...] = ()
            self._not_live: set[int] = set()  # quote requests IBKR said are not real-time
            self._void: set[int] = set()  # market requests voided by a fault
            self._claimed: set[int] = set()  # market request ids used in this session
            self._dropped_market = False

    def claim(self, req_id: int) -> None:
        """A market request id, used once per session: IBKR can still deliver a cancelled
        request's data after the cancel, so a reused id would take another contract's
        late ticks as its own. (A new session starts clean: IBKR forgot the old one.)"""
        if req_id < MARKET_REQUEST_BASE:
            raise ValueError(f"market request id {req_id} is below {MARKET_REQUEST_BASE}")
        with self._session_lock:
            if req_id in self._claimed:
                raise ValueError(f"market request id {req_id} was already used this session")
            self._claimed.add(req_id)

    def _is_void(self, req_id: int) -> bool:
        with self._session_lock:
            return req_id in self._void

    def _is_live(self, req_id: int) -> bool:
        with self._session_lock:
            return req_id not in self._not_live  # (void requests: _market_guard)

    def foreign_open(self) -> int:
        with self._session_lock:
            return len(self._foreign_live)

    def other_accounts(self) -> tuple[str, ...]:
        """Accounts on this login other than the system's."""
        with self._session_lock:
            return tuple(a for a in self._accounts if a != self._b._s.account)

    def closed(self) -> None:
        """The session ended (run returned, or the connect failed)."""
        with self._session_lock:
            was_open, self._open = self._open, False
        if was_open:
            try:
                self._b._forward().on_connection(Connection.LOST)
            finally:  # the market hears it even if the adapter failed
                self._tell_market("connection", lambda m: m.on_market_connection(Connection.LOST))

    def _guard(self, what: str, action: Callable[[], None]) -> None:
        try:
            action()
        except UnreadableValue as exc:
            self._b._alarm(f"IBKR sent an unreadable {what} ({exc}); reconnecting to resync")
            self._b.stop()

    def _market(self) -> IbMarketEvents | None:
        market = self._b._market_events()
        if market is None:
            with self._session_lock:
                first, self._dropped_market = not self._dropped_market, True
            if first:
                log.warning("IBKR market data arrived with no market service attached")
        return market

    def _market_guard(
        self, req_id: int, what: str, action: Callable[[IbMarketEvents], None]
    ) -> None:
        """A market data fault of any kind (an unreadable value, or a failure in the
        market service) voids that one request, once; it never touches the orders."""
        market = self._market()
        if market is None or self._is_void(req_id):
            return
        try:
            action(market)
        except Exception as exc:
            if isinstance(exc, UnreadableValue):
                message = f"IBKR sent an unreadable {what} ({exc})"
            else:
                log.exception("market data %s failed on request %s", what, req_id)
                message = f"handling a {what} failed ({exc!r})"
            with self._session_lock:
                self._void.add(req_id)  # its later callbacks are dropped until re-requested
            self._b._alarm(f"{message}; market request {req_id} is void")
            self._tell_market(
                "error", lambda m: m.on_market_error(req_id, MARKET_DATA_UNREADABLE, message)
            )

    def _tell_market(self, what: str, action: Callable[[IbMarketEvents], None]) -> None:
        """Tell the market service something that belongs to no one request."""
        market = self._b._market_events()
        if market is None:
            return
        try:
            action(market)
        except Exception as exc:
            log.exception("the market service failed on a %s", what)
            self._b._alarm(f"the market service failed on a {what} ({exc!r})")

    # --- the session ---------------------------------------------------------------

    def managedAccounts(self, accountsList: str) -> None:
        accounts = [a.strip() for a in accountsList.split(",") if a.strip()]
        s = self._b._s
        if s.account not in accounts:
            self._b._refuse(f"the login manages {accounts}, not {s.account}")
            return
        if s.require_paper and not s.account.startswith(s.paper_account_prefix):
            self._b._refuse(f"{s.account} is not a paper account and require_paper is set")
            return
        with self._session_lock:
            self._accounts_ok = True
            self._accounts = tuple(accounts)
        self._announce()

    def nextValidId(self, orderId: int) -> None:
        with self._session_lock:
            self._next_id = orderId
        self._announce()

    def _announce(self) -> None:
        with self._session_lock:
            if self._announced or not self._accounts_ok or self._next_id is None:
                return
            self._announced = self._open = True
            next_id = self._next_id
        # Real-time data only, set before anything is asked for in this session.
        self._b._call().reqMarketDataType(_REAL_TIME)
        self._b._forward().on_connection(Connection.CONNECTED, next_id)
        self._tell_market("connection", lambda m: m.on_market_connection(Connection.CONNECTED))

    def connectionClosed(self) -> None:
        self.closed()

    def error(
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        if advancedOrderRejectJson:
            log.warning("IBKR order reject detail on %s: %s", reqId, advancedOrderRejectJson)
        with self._session_lock:
            open_ = self._open
        if not open_:
            log.warning("IBKR %s before the session opened: %s", errorCode, errorString)
        elif reqId >= MARKET_REQUEST_BASE:  # a market data request's, never an order's
            if self._b._market_events() is None:
                log.warning("IBKR %s on market request %s: %s", errorCode, reqId, errorString)
            self._market_guard(
                reqId, "market error", lambda m: m.on_market_error(reqId, errorCode, errorString)
            )
        else:
            try:
                self._b._forward().on_error(reqId, errorCode, errorString)
            finally:
                if reqId < 0:  # system-wide: data farms, connectivity
                    self._tell_market(
                        "notice", lambda m: m.on_market_notice(errorCode, errorString)
                    )

    # --- orders ----------------------------------------------------------------------

    def openOrder(
        self,
        orderId: int,
        contract: Contract,
        order: Order,
        orderState: OrderState,
    ) -> None:
        if order.account != self._b._s.account:
            if not order.account:
                log.critical("IBKR order %s arrived with no account: ignored", order.permId)
            with self._session_lock:
                self._foreign.add(order.permId)
                if orderState.status in _FINISHED:
                    self._foreign_live.discard(order.permId)
                else:
                    self._foreign_live.add(order.permId)
            return
        # Order ids are unique only within a client: another client's id is never ours.
        ib_id = orderId if order.clientId == self._b._s.client_id else 0
        if (contract.symbol, contract.secType) != self._b._scope:
            with self._session_lock:
                self._outside.add(order.permId)
            # Passed on only so the adapter knows it is open; no number in it is read.
            self._b._forward().on_open_order(
                ib_id,
                order.permId,
                order.orderRef,
                contract.conId,
                contract.symbol,
                contract.secType,
                order.action,
                0,
                order.orderType,
                Decimal(0),
                None,
                order.ocaGroup or None,
                orderState.status,
            )
            return

        with self._session_lock:
            self._in_scope.add(order.permId)

        def forward() -> None:
            stop = to_price(order.auxPrice) if order.orderType in _STOP_TYPES else None
            self._b._forward().on_open_order(
                ib_id,
                order.permId,
                order.orderRef,
                contract.conId,
                contract.symbol,
                contract.secType,
                order.action,
                to_quantity(order.totalQuantity),
                order.orderType,
                to_price(order.lmtPrice) or Decimal(0),
                stop,
                order.ocaGroup or None,
                orderState.status,
            )

        self._guard("open order", forward)

    def openOrderEnd(self) -> None:
        self._b._forward().on_open_orders_end()

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Decimal,
        remaining: Decimal,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._session_lock:
            if permId in self._foreign:
                if status in _FINISHED:
                    self._foreign_live.discard(permId)
                return
            outside = permId in self._outside
            ours = clientId == self._b._s.client_id
            ib_id = orderId if ours else 0  # ids are per client: another's is never ours
            known = permId in self._in_scope or (ours and self._b._sent(orderId))

        def forward() -> None:
            # `remaining` is never used; an unset `filled` is unknown (the adapter
            # counts fills from the executions). For an order of the system's own
            # instrument (seen in scope, or sent by this binding), `filled` must be
            # read exactly. Outside the scope nothing is read; for an order not seen
            # yet, an unreadable `filled` (fractional shares) is passed as unknown.
            if outside or (isinstance(filled, Decimal) and filled == UNSET_DECIMAL):
                done = 0
            elif known:
                done = to_quantity(filled)
            else:
                try:
                    done = to_quantity(filled)
                except UnreadableValue:
                    done = 0
            self._b._forward().on_order_status(
                ib_id, permId, status, done, 0, to_price(avgFillPrice) or Decimal(0)
            )

        self._guard("order status", forward)

    # --- fills and positions ------------------------------------------------------------

    def execDetails(self, reqId: int, contract: Contract, execution: Execution) -> None:
        if execution.acctNumber != self._b._s.account:
            return
        if (contract.symbol, contract.secType) != self._b._scope:
            return  # not the system's: nothing in it is read

        def forward() -> None:
            price = to_price(execution.price)
            if price is None:
                raise UnreadableValue(f"execution {execution.execId} has no price")
            self._b._forward().on_execution(
                execution.execId,
                execution.orderId,
                execution.permId,
                contract.conId,
                contract.symbol,
                contract.secType,
                execution.side,
                to_quantity(execution.shares),
                price,
            )

        self._guard("execution", forward)

    def execDetailsEnd(self, reqId: int) -> None:
        self._b._forward().on_executions_end()

    def position(
        self,
        account: str,
        contract: Contract,
        position: Decimal,
        avgCost: float,
    ) -> None:
        if account != self._b._s.account:
            return
        if (contract.symbol, contract.secType) != self._b._scope:
            return  # not the system's: nothing in it is read
        self._guard(
            "position",
            lambda: self._b._forward().on_position(
                contract.conId, contract.symbol, contract.secType, to_quantity(position)
            ),
        )

    def positionEnd(self) -> None:
        self._b._forward().on_positions_end()

    # --- market data -------------------------------------------------------------------

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:
        self._market_guard(
            reqId,
            "contract",
            lambda m: m.on_contract_details(reqId, from_details(contractDetails)),
        )

    def contractDetailsEnd(self, reqId: int) -> None:
        self._market_guard(reqId, "contract", lambda m: m.on_contract_details_end(reqId))

    def securityDefinitionOptionParameter(
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations: Any,
        strikes: Any,
    ) -> None:
        def forward(market: IbMarketEvents) -> None:
            exact = frozenset(to_price(s) for s in strikes)
            if None in exact:
                raise UnreadableValue(f"option strikes for {tradingClass} are unreadable")
            market.on_option_params(
                reqId,
                exchange,
                tradingClass,
                multiplier,
                frozenset(str(e) for e in expirations),
                frozenset(s for s in exact if s is not None),
            )

        self._market_guard(reqId, "option chain", forward)

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
        self._market_guard(reqId, "option chain", lambda m: m.on_option_params_end(reqId))

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        """IBKR says what kind of data a quote request gets. Frozen data comes on the
        ordinary bid and ask ticks, so anything but real-time silences that request."""
        if self._market() is None or self._is_void(reqId):
            return
        with self._session_lock:
            if marketDataType == _REAL_TIME:
                self._not_live.discard(reqId)
                return
            self._not_live.add(reqId)
        message = f"IBKR is sending market data type {marketDataType}, not real-time"
        self._b._alarm(f"{message} for market request {reqId}; its quotes are ignored")
        self._market_guard(
            reqId,
            "market data type",
            lambda m: m.on_market_error(reqId, MARKET_DATA_NOT_REAL_TIME, message),
        )

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: TickAttrib) -> None:
        field = _QUOTE_PRICES.get(tickType)  # real-time bid and ask only; delayed never
        if field is None or not self._is_live(reqId):
            return
        value = to_price(price)
        quote = value if value is not None and value > 0 else None
        self._market_guard(reqId, "quote", lambda m: m.on_quote(reqId, field, quote))

    def tickSize(self, reqId: int, tickType: int, size: Decimal) -> None:
        if tickType not in _OPEN_INTEREST or not self._is_live(reqId):
            return
        self._market_guard(
            reqId,
            "open interest",
            lambda m: m.on_quote(reqId, QuoteField.OPEN_INTEREST, Decimal(to_quantity(size))),
        )

    def historicalData(self, reqId: int, bar: BarData) -> None:
        self._market_guard(reqId, "bar", lambda m: m.on_bar(reqId, to_bar(bar)))

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self._market_guard(reqId, "bar", lambda m: m.on_bars_end(reqId))

    def historicalDataUpdate(self, reqId: int, bar: BarData) -> None:
        self._market_guard(reqId, "bar", lambda m: m.on_bar_update(reqId, to_bar(bar)))
