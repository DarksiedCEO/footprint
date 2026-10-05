"""The binding: our requests become ibapi calls, IBKR's callbacks become IbEvents."""

import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
    pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")

from ibapi.const import UNSET_DECIMAL, UNSET_DOUBLE
from ibapi.contract import Contract
from ibapi.execution import Execution
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.order_state import OrderState

from footprint.brokers.ibkr.adapter import ContractBook, IbkrBroker, IbkrSettings
from footprint.brokers.ibkr.binding import (
    ForeignOrdersOpen,
    IbBinding,
    IbConnectionSettings,
    UnreadableValue,
    build_order,
    read_env_file,
    to_price,
    to_quantity,
)
from footprint.brokers.ibkr.gateway import Connection, IbContract, IbOrderRequest
from footprint.core.config import IbkrConfig

D = Decimal
ACCOUNT = "DUR239136"
SCOPE = ("NQ", "FOP")
NQ_CALL = IbContract(501, "NQ", "FOP", "CME", "USD", "20260916", D(23725), "C", "20", "Q3C")


def settings(**changes: Any) -> IbConnectionSettings:
    base: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 4002,
        "client_id": 0,
        "account": ACCOUNT,
        "reconnect_seconds": 0.01,
        "require_paper": True,
        "paper_account_prefix": "DU",
    }
    base.update(changes)
    return IbConnectionSettings(**base)


@dataclass
class Recording:
    """ibapi's EClient, recorded. `connect` succeeds unless told otherwise; `run`
    returns at once (the connection drops)."""

    wrapper: Any
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    connected: bool = False
    connect_ok: bool = True
    on_run: Any = None

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.calls.append(("connect", (host, port, client_id)))
        self.connected = self.connect_ok

    def isConnected(self) -> bool:
        return self.connected

    def run(self) -> None:
        self.calls.append(("run", ()))
        if self.on_run is not None:
            self.on_run(self)
        self.disconnect()

    def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            self.wrapper.connectionClosed()

    def __getattr__(self, name: str) -> Any:
        return lambda *args: self.calls.append((name, args))


class Events:
    """IbEvents, recorded."""

    def __init__(self) -> None:
        self.got: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        return lambda *args: self.got.append((name, args))


def bound(**changes: Any) -> tuple[IbBinding, Recording, Events, list[str]]:
    alarms: list[str] = []
    clients: list[Recording] = []

    def factory(wrapper: Any) -> Recording:
        clients.append(Recording(wrapper))
        return clients[-1]

    binding = IbBinding(settings(**changes), SCOPE, alarms.append, factory)
    events = Events()
    binding.attach(events)
    binding._client = factory(binding._wrapper)
    binding._client.connected = True
    return binding, binding._client, events, alarms


def opened(**changes: Any) -> tuple[IbBinding, Recording, Events, list[str]]:
    binding, client, events, alarms = bound(**changes)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(100)
    events.got.clear()
    return binding, client, events, alarms


def request() -> IbOrderRequest:
    return IbOrderRequest(
        7, NQ_CALL, "SELL", 1, "STP LMT", D(109), D("114.25"), "p1-stop", "p1", 1, "DAY"
    )


# --- what goes out ---------------------------------------------------------------------


def test_a_stop_limit_goes_out_exactly_as_asked() -> None:
    binding, client, _, _ = opened()
    binding.place_order(request())
    ((name, (ib_id, contract, order)),) = client.calls
    assert (name, ib_id) == ("placeOrder", 7)
    assert (contract.conId, contract.symbol, contract.secType, contract.exchange) == (
        501,
        "NQ",
        "FOP",
        "CME",
    )
    assert (contract.lastTradeDateOrContractMonth, contract.strike, contract.right) == (
        "20260916",
        23725.0,
        "C",
    )
    assert (contract.multiplier, contract.tradingClass, contract.currency) == ("20", "Q3C", "USD")
    assert (order.action, order.totalQuantity, order.orderType) == ("SELL", D(1), "STP LMT")
    assert (order.lmtPrice, order.auxPrice) == (109.0, 114.25)
    assert (order.tif, order.orderRef, order.ocaGroup, order.ocaType) == ("DAY", "p1-stop", "p1", 1)
    assert order.account == ACCOUNT  # never another account on the login
    assert order.transmit is True
    assert order.outsideRth is True  # a stop that triggers overnight too


def test_a_limit_order_carries_no_stop_price_and_no_group() -> None:
    plain = IbOrderRequest(
        8, NQ_CALL, "BUY", 1, "LMT", D("151.5"), None, "p1-entry", None, None, "DAY"
    )
    order = build_order(plain, ACCOUNT)
    assert order.auxPrice == UNSET_DOUBLE
    assert order.lmtPrice == 151.5
    assert order.ocaGroup == ""


def test_cancels_and_requests_use_ibapi_10_50_calls() -> None:
    binding, client, _, _ = opened()
    binding.cancel_order(7)
    binding.global_cancel()
    binding.request_open_orders()
    binding.request_executions()
    binding.request_executions()
    binding.request_positions()
    names = [name for name, _ in client.calls]
    assert names == [
        "cancelOrder",
        "reqGlobalCancel",
        "reqAllOpenOrders",
        "reqExecutions",
        "reqExecutions",
        "cancelPositions",  # a retaken snapshot is a fresh subscription
        "reqPositions",
    ]
    assert client.calls[0][1][0] == 7
    assert isinstance(client.calls[0][1][1], OrderCancel)
    assert isinstance(client.calls[1][1][0], OrderCancel)
    (first, filt), (second, _) = client.calls[3][1], client.calls[4][1]
    assert second == first + 1  # each replay has its own request id
    assert filt.acctCode == ACCOUNT


def test_nothing_is_sent_without_a_connection() -> None:
    binding = IbBinding(settings(), SCOPE)
    with pytest.raises(ConnectionError):
        binding.place_order(request())


# --- the session ---------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["accounts", "next_id"])
def test_connected_is_told_once_after_the_account_and_the_next_id(first: str) -> None:
    binding, _, events, _ = bound()
    steps = {
        "accounts": lambda: binding._wrapper.managedAccounts(f"{ACCOUNT},DU999"),
        "next_id": lambda: binding._wrapper.nextValidId(100),
    }
    steps[first]()
    assert events.got == []  # not until both are in
    steps["next_id" if first == "accounts" else "accounts"]()
    binding._wrapper.nextValidId(101)  # IBKR answering reqIds later: not a new session
    assert events.got == [("on_connection", (Connection.CONNECTED, 100))]


def test_a_live_account_is_refused_and_never_retried() -> None:
    binding, client, events, alarms = bound(account="U1234567")
    binding._wrapper.managedAccounts("U1234567")
    binding._wrapper.nextValidId(100)
    assert events.got == []  # the adapter never hears CONNECTED
    assert binding.refused is not None
    assert not client.connected
    assert any("REFUSED" in a and "paper" in a for a in alarms)
    made: list[Any] = []
    binding._factory = lambda wrapper: made.append(wrapper)
    binding.run(threading.Event())  # returns at once: never connects again
    assert made == []


def test_a_login_without_the_configured_account_is_refused() -> None:
    binding, _, events, alarms = bound()
    binding._wrapper.managedAccounts("DU0000001")
    binding._wrapper.nextValidId(100)
    assert events.got == []
    assert any(ACCOUNT in a for a in alarms)


def test_going_live_needs_the_paper_guard_turned_off() -> None:
    binding, _, events, _ = bound(account="U1234567", require_paper=False)
    binding._wrapper.managedAccounts("U1234567")
    binding._wrapper.nextValidId(5)
    assert events.got == [("on_connection", (Connection.CONNECTED, 5))]


def test_a_dropped_connection_is_told_once() -> None:
    binding, client, events, _ = opened()
    client.disconnect()
    binding._wrapper.closed()  # the supervisor's own check after run returns
    assert events.got == [("on_connection", (Connection.LOST,))]


def test_errors_are_forwarded_once_the_session_is_open() -> None:
    binding, _, events, _ = bound()
    binding._wrapper.error(-1, 0, 502, "Couldn't connect to TWS")  # before: logged only
    assert events.got == []
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(1)
    events.got.clear()
    binding._wrapper.error(7, 1759700000000, 201, "Order rejected", '{"reason": "x"}')
    binding._wrapper.error(-1, 1759700000000, 1100, "Connectivity lost")
    assert events.got == [
        ("on_error", (7, 201, "Order rejected")),
        ("on_error", (-1, 1100, "Connectivity lost")),
    ]


# --- what comes in -------------------------------------------------------------------------


def ib_contract() -> Contract:
    c = Contract()
    c.conId, c.symbol, c.secType = 501, "NQ", "FOP"
    return c


def ib_order(order_type: str = "STP LMT", account: str = ACCOUNT, perm: int = 9001) -> Order:
    o = Order()
    o.permId, o.orderRef, o.action = perm, "p1-stop", "SELL"
    o.totalQuantity, o.orderType = D(1), order_type
    o.lmtPrice, o.auxPrice = 109.0, 114.25
    o.ocaGroup, o.account = "p1", account
    return o


def state(status: str) -> OrderState:
    s = OrderState()
    s.status = status
    return s


def test_an_open_order_arrives_with_exact_prices() -> None:
    binding, _, events, _ = opened()
    binding._wrapper.openOrder(7, ib_contract(), ib_order(), state("Submitted"))
    assert events.got == [
        (
            "on_open_order",
            (
                7,
                9001,
                "p1-stop",
                501,
                "NQ",
                "FOP",
                "SELL",
                1,
                "STP LMT",
                D(109),
                D("114.25"),
                "p1",
                "Submitted",
            ),
        )
    ]


def test_a_limit_order_arrives_without_a_stop_and_an_empty_group_as_none() -> None:
    binding, _, events, _ = opened()
    order = ib_order("LMT")
    order.ocaGroup, order.auxPrice = "", UNSET_DOUBLE
    binding._wrapper.openOrder(8, ib_contract(), order, state("PreSubmitted"))
    args = events.got[0][1]
    assert (args[9], args[10], args[11]) == (D(109), None, None)


def test_order_status_and_execution_arrive_as_whole_contracts_and_exact_prices() -> None:
    binding, _, events, _ = opened()
    binding._wrapper.openOrder(7, ib_contract(), ib_order(), state("Submitted"))
    events.got.clear()
    binding._wrapper.orderStatus(7, "Filled", D(1), D(0), 114.25, 9001, 0, 114.25, 0, "", 0.0)
    e = Execution()
    e.execId, e.orderId, e.permId = "0000e0d5.65a1b2c3.01.01", 7, 9001
    e.side, e.shares, e.price, e.acctNumber = "SLD", D(1), 114.25, ACCOUNT
    binding._wrapper.execDetails(-1, ib_contract(), e)
    binding._wrapper.execDetailsEnd(3)
    binding._wrapper.position(ACCOUNT, ib_contract(), D(-1), 0.0)
    binding._wrapper.positionEnd()
    binding._wrapper.openOrderEnd()
    assert events.got == [
        ("on_order_status", (7, 9001, "Filled", 1, 0, D("114.25"))),
        (
            "on_execution",
            ("0000e0d5.65a1b2c3.01.01", 7, 9001, 501, "NQ", "FOP", "SLD", 1, D("114.25")),
        ),
        ("on_executions_end", ()),
        ("on_position", (501, "NQ", "FOP", -1)),
        ("on_positions_end", ()),
        ("on_open_orders_end", ()),
    ]


def test_another_account_on_the_login_never_reaches_the_adapter() -> None:
    binding, _, events, _ = opened()
    binding._wrapper.openOrder(
        9, ib_contract(), ib_order(account="DU0000002", perm=77), state("Submitted")
    )
    binding._wrapper.orderStatus(9, "Filled", D(1), D(0), 1.0, 77, 0, 1.0, 0, "", 0.0)
    e = Execution()
    e.execId, e.permId, e.side, e.shares, e.price = "x.01", 77, "BOT", D(1), 1.0
    e.acctNumber = "DU0000002"
    binding._wrapper.execDetails(-1, ib_contract(), e)
    binding._wrapper.position("DU0000002", ib_contract(), D(3), 0.0)
    assert events.got == []


def test_an_unreadable_number_drops_the_connection_rather_than_guess() -> None:
    binding, client, events, alarms = opened()
    binding._wrapper.position(ACCOUNT, ib_contract(), D("0.5"), 0.0)
    assert not any(name == "on_position" for name, _ in events.got)
    assert not client.connected  # the reconnect's resync reads the account afresh
    assert any("unreadable position" in a for a in alarms)


def test_numbers_are_read_exactly_or_not_at_all() -> None:
    assert to_price(151.25) == D("151.25")
    assert to_price(0.1) == D("0.1")  # its shortest form, not the binary expansion
    assert to_price(UNSET_DOUBLE) is None
    assert to_price(float("nan")) is None
    assert to_quantity(D(2)) == 2
    assert to_quantity(D("-1")) == -1
    for bad in (D("1.5"), UNSET_DECIMAL, D("NaN")):
        with pytest.raises(UnreadableValue):
            to_quantity(bad)


# --- the supervisor --------------------------------------------------------------------------


def test_the_supervisor_reconnects_after_a_drop_until_stopped() -> None:
    alarms: list[str] = []
    clients: list[Recording] = []
    stop = threading.Event()
    binding: IbBinding

    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper)

        def session(c: Recording) -> None:
            wrapper.managedAccounts(ACCOUNT)
            wrapper.nextValidId(100 + len(clients))
            if len(clients) == 3:
                stop.set()

        client.on_run = session
        clients.append(client)
        return client

    binding = IbBinding(settings(reconnect_seconds=0.05), SCOPE, alarms.append, factory)
    events = Events()
    binding.attach(events)
    started = time.monotonic()
    binding.run(stop)
    assert time.monotonic() - started >= 0.1  # it waited between attempts, twice
    assert len(clients) == 3  # reconnected twice, then stopped
    assert [c.calls[0] for c in clients] == [("connect", ("127.0.0.1", 4002, 0))] * 3
    states = [args[0] for name, args in events.got if name == "on_connection"]
    assert states == [Connection.CONNECTED, Connection.LOST] * 3


def test_a_failed_connect_is_retried_without_telling_the_adapter_anything() -> None:
    clients: list[Recording] = []
    stop = threading.Event()

    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper, connect_ok=False)
        clients.append(client)
        if len(clients) == 2:
            stop.set()
        return client

    binding = IbBinding(settings(), SCOPE, client_factory=factory)
    events = Events()
    binding.attach(events)
    binding.run(stop)
    assert len(clients) == 2
    assert events.got == []
    assert all(("run", ()) not in c.calls for c in clients)  # a dead client is not run


# --- wired to the adapter ------------------------------------------------------------------


def test_the_adapter_resyncs_through_the_binding_in_order() -> None:
    binding, client, _, alarms = bound()
    contracts = ContractBook()
    adapter = IbkrBroker(
        binding, contracts, IbkrSettings.from_config(ibkr_config()), ("NQ", "FOP"), alarms.append
    )
    binding.attach(adapter)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(100)
    assert [n for n, _ in client.calls] == ["reqAllOpenOrders", "reqExecutions"]
    binding._wrapper.openOrderEnd()
    binding._wrapper.execDetailsEnd(1)
    assert [n for n, _ in client.calls][-2:] == ["cancelPositions", "reqPositions"]
    binding._wrapper.position(ACCOUNT, ib_contract(), D(1), 0.0)
    binding._wrapper.positionEnd()
    assert adapter.positions() == {"conid:501": 1}  # ready, and the holding is seen


def ibkr_config() -> IbkrConfig:
    return IbkrConfig(
        ack_timeout_seconds=1,
        positions_disagree_alarm_seconds=1,
        reconnect_seconds=1,
        option_security_type="FOP",
        require_paper=True,
        paper_account_prefix="DU",
    )


# --- settings ------------------------------------------------------------------------------


def test_settings_come_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nBROKER_HOST=127.0.0.1\nBROKER_PORT=4002\n"
        f"BROKER_ACCOUNT_ID={ACCOUNT}\nBROKER_CLIENT_ID=0\n"
    )
    s = IbConnectionSettings.from_env(read_env_file(str(env_file)), ibkr_config())
    assert (s.host, s.port, s.client_id, s.account, s.require_paper) == (
        "127.0.0.1",
        4002,
        0,
        ACCOUNT,
        True,
    )


def test_missing_or_malformed_settings_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BROKER_ACCOUNT_ID"):
        IbConnectionSettings.from_env(
            {"BROKER_HOST": "h", "BROKER_PORT": "1", "BROKER_CLIENT_ID": "0"}, ibkr_config()
        )
    with pytest.raises(ValueError, match="whole numbers"):
        IbConnectionSettings.from_env(
            {
                "BROKER_HOST": "h",
                "BROKER_PORT": "x",
                "BROKER_ACCOUNT_ID": "a",
                "BROKER_CLIENT_ID": "0",
            },
            ibkr_config(),
        )
    bad = tmp_path / ".env"
    bad.write_text("JUSTTEXT\n")
    with pytest.raises(ValueError, match="KEY=VALUE"):
        read_env_file(str(bad))


def test_the_supervisor_stops_for_good_when_the_account_is_refused() -> None:
    clients: list[Recording] = []

    def factory(wrapper: Any) -> Recording:
        client = Recording(wrapper)

        def session(c: Recording) -> None:
            wrapper.managedAccounts("U1234567")  # a live account behind the paper port
            wrapper.nextValidId(1)

        client.on_run = session
        clients.append(client)
        return client

    binding = IbBinding(settings(account="U1234567"), SCOPE, client_factory=factory)
    events = Events()
    binding.attach(events)
    worker = threading.Thread(target=binding.run, args=(threading.Event(),), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()  # returned without being stopped
    assert len(clients) == 1  # and never tried again
    assert events.got == []


def test_a_limit_order_with_a_zero_aux_price_has_no_stop() -> None:
    binding, _, events, _ = opened()
    order = ib_order("LMT")
    order.auxPrice = 0.0  # IBKR often reports an unused aux price as 0
    binding._wrapper.openOrder(8, ib_contract(), order, state("Submitted"))
    assert events.got[0][1][10] is None


# --- review round 1 -----------------------------------------------------------------------


def stock() -> Contract:
    c = Contract()
    c.conId, c.symbol, c.secType = 900, "XYZ", "STK"
    return c


def test_a_fractional_holding_outside_the_scope_never_drops_the_connection() -> None:
    binding, client, events, alarms = opened()
    binding._wrapper.position(ACCOUNT, stock(), D("10.5"), 0.0)  # dividend reinvestment
    e = Execution()
    e.execId, e.permId, e.side, e.shares, e.price = "s.01", 55, "BOT", D("0.5"), 101.0
    e.acctNumber = ACCOUNT
    binding._wrapper.execDetails(-1, stock(), e)
    fractional = ib_order("LMT", perm=55)
    fractional.totalQuantity, fractional.lmtPrice = D("2.5"), UNSET_DOUBLE
    binding._wrapper.openOrder(0, stock(), fractional, state("Submitted"))
    binding._wrapper.orderStatus(0, "Submitted", D("0.5"), D(2), 0.0, 55, 0, 0.0, 0, "", 0.0)
    assert client.connected
    assert alarms == []
    names = [name for name, _ in events.got]
    assert names == ["on_open_order", "on_order_status"]  # only so the adapter knows it is open
    assert events.got[0][1][4:6] == ("XYZ", "STK")


def test_a_broker_wide_cancel_is_refused_while_another_accounts_order_is_open() -> None:
    binding, client, _, _ = opened()
    binding._wrapper.openOrder(
        9, ib_contract(), ib_order(account="DU0000002", perm=77), state("Submitted")
    )
    with pytest.raises(ForeignOrdersOpen):
        binding.global_cancel()
    assert not any(name == "reqGlobalCancel" for name, _ in client.calls)
    binding._wrapper.orderStatus(9, "Cancelled", D(0), D(1), 0.0, 77, 0, 0.0, 0, "", 0.0)
    binding.global_cancel()  # nothing of theirs is open any more
    assert any(name == "reqGlobalCancel" for name, _ in client.calls)


def test_the_adapter_asks_for_a_hand_cancel_when_the_broker_wide_cancel_is_refused() -> None:
    binding, _, _, alarms = bound()
    adapter = IbkrBroker(
        binding, ContractBook(), IbkrSettings.from_config(ibkr_config()), SCOPE, alarms.append
    )
    binding.attach(adapter)
    binding._wrapper.managedAccounts(ACCOUNT)
    binding._wrapper.nextValidId(100)
    binding._wrapper.openOrder(
        9, ib_contract(), ib_order(account="DU0000002", perm=77), state("Submitted")
    )
    hand = ib_order("LMT", perm=88)
    hand.orderRef = ""  # placed by hand in TWS: only a broker-wide cancel reaches it
    binding._wrapper.openOrder(0, ib_contract(), hand, state("Submitted"))
    binding._wrapper.openOrderEnd()
    binding._wrapper.execDetailsEnd(1)
    binding._wrapper.positionEnd()
    status = adapter.cancel("ib-88")
    assert status.order.order_id == "ib-88"
    assert any("CANCEL BY HAND" in a for a in alarms)


def test_a_status_with_unset_quantities_keeps_the_connection() -> None:
    binding, client, events, _ = opened()
    binding._wrapper.orderStatus(
        7, "Submitted", UNSET_DECIMAL, UNSET_DECIMAL, 0.0, 9001, 0, 0.0, 0, "", 0.0
    )
    assert client.connected
    assert events.got == [("on_order_status", (7, 9001, "Submitted", 0, 0, D(0)))]


def test_a_fractional_fill_on_the_systems_own_instrument_is_unreadable() -> None:
    binding, client, _, alarms = opened()
    binding._wrapper.openOrder(7, ib_contract(), ib_order(), state("Submitted"))
    binding._wrapper.orderStatus(7, "Filled", D("0.5"), D("0.5"), 1.0, 9001, 0, 1.0, 0, "", 0.0)
    assert not client.connected
    assert any("unreadable order status" in a for a in alarms)


def test_anything_with_no_account_never_reaches_the_adapter() -> None:
    binding, _, events, _ = opened()
    binding._wrapper.openOrder(9, ib_contract(), ib_order(account="", perm=66), state("Submitted"))
    e = Execution()
    e.execId, e.permId, e.side, e.shares, e.price = "n.01", 66, "BOT", D(1), 1.0
    binding._wrapper.execDetails(-1, ib_contract(), e)  # acctNumber left empty
    assert events.got == []


def test_another_accounts_finished_order_does_not_block_the_broker_wide_cancel() -> None:
    binding, client, _, _ = opened()
    binding._wrapper.openOrder(
        9, ib_contract(), ib_order(account="DU0000002", perm=78), state("Filled")
    )
    binding.global_cancel()
    assert any(name == "reqGlobalCancel" for name, _ in client.calls)


# --- review round 2 -----------------------------------------------------------------------


def test_a_login_with_other_accounts_never_sends_a_broker_wide_cancel() -> None:
    binding, client, _, _ = bound()
    binding._wrapper.managedAccounts(f"{ACCOUNT},DU0000002")
    binding._wrapper.nextValidId(100)
    with pytest.raises(ForeignOrdersOpen, match="DU0000002"):
        binding.global_cancel()  # even with none of their orders seen
    assert not any(name == "reqGlobalCancel" for name, _ in client.calls)


def test_a_status_for_an_order_not_yet_seen_reads_no_quantity() -> None:
    binding, client, events, _ = opened()
    binding._wrapper.orderStatus(0, "Submitted", D("0.5"), D(2), 0.0, 4242, 0, 0.0, 0, "", 0.0)
    assert client.connected
    assert events.got == [("on_order_status", (0, 4242, "Submitted", 0, 0, D(0)))]


def test_another_accounts_inactive_order_still_blocks_the_broker_wide_cancel() -> None:
    binding, _, _, _ = opened()
    binding._wrapper.openOrder(
        9, ib_contract(), ib_order(account="DU0000002", perm=79), state("Inactive")
    )
    with pytest.raises(ForeignOrdersOpen):
        binding.global_cancel()


# --- review round 3 -----------------------------------------------------------------------


def test_our_own_fill_told_before_its_open_order_keeps_its_quantity() -> None:
    binding, _, events, _ = opened()
    binding.place_order(request())  # ib id 7, sent by this binding
    binding._wrapper.orderStatus(7, "Filled", D(1), D(0), 114.25, 9001, 0, 114.25, 0, "", 0.0)
    assert events.got == [("on_order_status", (7, 9001, "Filled", 1, 0, D("114.25")))]


def test_a_fractional_fill_on_our_own_order_is_unreadable_even_before_its_open_order() -> None:
    binding, client, _, alarms = opened()
    binding.place_order(request())
    binding._wrapper.orderStatus(7, "Filled", D("0.5"), D(0), 1.0, 9001, 0, 1.0, 0, "", 0.0)
    assert not client.connected
    assert any("unreadable order status" in a for a in alarms)


def test_an_unseen_orders_whole_fill_is_passed_on() -> None:
    binding, _, events, _ = opened()
    binding._wrapper.orderStatus(3, "Filled", D(2), D(0), 1.5, 4343, 0, 1.5, 0, "", 0.0)
    assert events.got == [("on_order_status", (3, 4343, "Filled", 2, 0, D("1.5")))]


def test_another_clients_stock_order_sharing_our_order_id_reads_no_quantity() -> None:
    """Order ids are per client: Andre's TWS order can carry the same id as one of ours."""
    binding, client, events, _ = opened()
    binding.place_order(request())  # our ib id 7
    fractional = ib_order("LMT", perm=56)
    fractional.totalQuantity = D("2.5")
    binding._wrapper.openOrder(7, stock(), fractional, state("Submitted"))
    binding._wrapper.orderStatus(7, "Submitted", D("0.5"), D(2), 0.0, 56, 0, 0.0, 0, "", 0.0)
    assert client.connected
    assert events.got[-1] == ("on_order_status", (7, 56, "Submitted", 0, 0, D(0)))
