"""The read-only first look: it reports what the system would see, and can send nothing."""

import io
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
    pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")

from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_state import OrderState

from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings
from footprint.brokers.ibkr.check import ReadOnlyGateway, check
from footprint.core.config import Profiles, load_profiles
from tests.brokers.test_ibkr_binding import ACCOUNT, request, settings

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def profiles() -> Profiles:
    return load_profiles(ROOT / "config", "nq.yaml")


def contract(symbol: str, sec_type: str, con_id: int) -> Contract:
    c = Contract()
    c.conId, c.symbol, c.secType = con_id, symbol, sec_type
    return c


def order(perm: int, ref: str, account: str = ACCOUNT) -> Order:
    o = Order()
    o.permId, o.orderRef, o.action, o.totalQuantity = perm, ref, "SELL", D(1)
    o.orderType, o.lmtPrice, o.account = "LMT", 300.0, account
    return o


class SimulatedGateway:
    """A tiny IB Gateway: answers the resync requests; records anything else."""

    def __init__(self, wrapper: Any, accounts: str = ACCOUNT, answer: bool = True) -> None:
        self.wrapper = wrapper
        self.accounts = accounts
        self.answer = answer
        self.connected = False
        self.sent: list[str] = []

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.connected = True

    def isConnected(self) -> bool:
        return self.connected

    def run(self) -> None:
        self.wrapper.managedAccounts(self.accounts)
        self.wrapper.nextValidId(1)
        while self.connected:
            time.sleep(0.005)

    def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            self.wrapper.connectionClosed()

    def reqAllOpenOrders(self) -> None:
        if not self.answer:
            return
        state = OrderState()
        state.status = "Submitted"
        self.wrapper.openOrder(5, contract("NQ", "FOP", 501), order(9001, "p1-stop"), state)
        self.wrapper.openOrder(0, contract("XYZ", "STK", 900), order(9002, ""), state)
        self.wrapper.openOrderEnd()

    def reqExecutions(self, req_id: int, execution_filter: Any) -> None:
        if self.answer:
            self.wrapper.execDetailsEnd(req_id)

    def cancelPositions(self) -> None:
        pass

    def reqPositions(self) -> None:
        self.wrapper.position(ACCOUNT, contract("NQ", "FOP", 501), D(1), 0.0)
        self.wrapper.position(ACCOUNT, contract("XYZ", "STK", 900), D(5), 0.0)
        self.wrapper.positionEnd()

    def __getattr__(self, name: str) -> Any:
        return lambda *args: self.sent.append(name)


def run_check(
    profiles: Profiles, s: IbConnectionSettings, wait: float = 2.0, **gateway: Any
) -> tuple[int, str, list[SimulatedGateway]]:
    gateways: list[SimulatedGateway] = []

    def factory(wrapper: Any) -> SimulatedGateway:
        gateways.append(SimulatedGateway(wrapper, **gateway))
        return gateways[-1]

    out = io.StringIO()
    code = check(
        profiles, s, wait, out, lambda st, scope, alarm: IbBinding(st, scope, alarm, factory)
    )
    return code, out.getvalue(), gateways


def test_the_check_reports_what_the_system_would_see(profiles: Profiles) -> None:
    code, out, gateways = run_check(profiles, settings())
    assert code == 0
    assert "resynced" in out
    assert "positions in scope: {'conid:501': 1}" in out  # the stock is not the system's
    assert "open orders in scope: 1" in out
    assert "p1-stop" in out
    assert "open orders on anything else (never touched): 1" in out
    assert all(not g.connected for g in gateways)  # disconnected at the end
    assert all(g.sent == [] for g in gateways)  # nothing but the resync was asked


def test_the_check_refuses_a_live_account(profiles: Profiles) -> None:
    code, out, _ = run_check(profiles, settings(account="U1234567"), accounts="U1234567")
    assert code == 1
    assert "REFUSED" in out


def test_the_check_says_when_the_resync_never_completes(profiles: Profiles) -> None:
    code, out, _ = run_check(profiles, settings(), wait=0.3, answer=False)
    assert code == 1
    assert "NOT READY" in out


def test_the_check_can_send_nothing() -> None:
    sent: list[str] = []

    class Inner:
        def __getattr__(self, name: str) -> Any:
            return lambda *args: sent.append(name)

    gateway = ReadOnlyGateway(Inner())
    with pytest.raises(PermissionError):
        gateway.place_order(request())
    with pytest.raises(PermissionError):
        gateway.cancel_order(1)
    with pytest.raises(PermissionError):
        gateway.global_cancel()
    gateway.request_open_orders()
    gateway.request_executions()
    gateway.request_positions()
    assert sent == ["request_open_orders", "request_executions", "request_positions"]
