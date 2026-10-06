import ipaddress
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from footprint.core.config import (
    ExecutionProfile,
    InstrumentProfile,
    RiskProfile,
    StrategyProfile,
    load_execution,
    load_instrument,
    load_risk,
    load_strategy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


class NetworkBlocked(OSError):
    """A test tried to reach the network (Twilio, Telegram, IBKR or anything else)."""


def _loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No test reaches beyond this machine (remediation item 4): no proxy (it would
    carry a request out), no name lookup and no connection to anything but loopback.
    Every attempt raises NetworkBlocked and is listed. Real delivery is checked only by
    the explicitly run `python -m footprint.live.alarm_setup test`."""
    attempts: list[str] = []
    # A proxy can listen on loopback: its endpoint is blocked too (it would carry a
    # request out), as well as being dropped from the environment.
    proxies: set[tuple[str, int]] = set()
    for name in _PROXY_VARS:
        url = urllib.parse.urlparse(os.environ.get(name, ""))
        if url.hostname and url.port:
            proxies.add((url.hostname, url.port))
        monkeypatch.delenv(name, raising=False)
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _loopback(host):
            attempts.append(f"lookup {host}")
            raise NetworkBlocked(f"network blocked in tests: lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(address, (str, bytes)):  # a unix socket path: local
            return
        if not _loopback(host) or tuple(address[:2]) in proxies:
            attempts.append(f"connect {host}:{address[1]}")
            raise NetworkBlocked(f"network blocked in tests: connect to {host!r}")

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    real_sendto = socket.socket.sendto

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        _check(args[-1])  # (data, address) or (data, flags, address): no UDP out either
        return real_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    return attempts


@pytest.fixture
def instrument_path() -> Path:
    return REPO_ROOT / "config" / "instruments" / "nq.yaml"


@pytest.fixture
def risk_path() -> Path:
    return REPO_ROOT / "config" / "risk.yaml"


@pytest.fixture
def strategy_path() -> Path:
    return REPO_ROOT / "config" / "strategy.yaml"


@pytest.fixture
def execution_path() -> Path:
    return REPO_ROOT / "config" / "execution.yaml"


@pytest.fixture
def execution(execution_path: Path) -> ExecutionProfile:
    return load_execution(execution_path)


@pytest.fixture
def instrument(instrument_path: Path) -> InstrumentProfile:
    return load_instrument(instrument_path)


@pytest.fixture
def risk(risk_path: Path) -> RiskProfile:
    return load_risk(risk_path)


@pytest.fixture
def strategy(strategy_path: Path) -> StrategyProfile:
    return load_strategy(strategy_path)
