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


def require_ibapi() -> None:
    """A test that needs IBKR's ibapi (the binding, the live program): skipped where
    ibapi is not installed (the cloud sandbox cannot download it), but required in CI,
    which sets FOOTPRINT_REQUIRE_IBAPI so a missing ibapi fails there, never skips."""
    if not os.environ.get("FOOTPRINT_REQUIRE_IBAPI"):
        pytest.importorskip("ibapi", reason="ibapi not installed: scripts/install_ibapi.py")


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
    # Every other resolver call too (independent audit A-12): by name or by address.
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        real = getattr(socket, name)

        def by_host(host: Any, *args: Any, _real: Any = real, _name: str = name) -> Any:
            if not _loopback(host):
                attempts.append(f"lookup {host}")
                raise NetworkBlocked(f"network blocked in tests: {_name}({host!r})")
            return _real(host, *args)

        monkeypatch.setattr(socket, name, by_host)
    real_getnameinfo = socket.getnameinfo

    def getnameinfo(address: Any, flags: int) -> Any:
        if not _loopback(address[0]):
            attempts.append(f"lookup {address[0]}")
            raise NetworkBlocked(f"network blocked in tests: getnameinfo({address[0]!r})")
        return real_getnameinfo(address, flags)

    monkeypatch.setattr(socket, "getnameinfo", getnameinfo)
    real_sendto = socket.socket.sendto

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        _check(args[-1])  # (data, address) or (data, flags, address): no UDP out either
        return real_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    real_sendmsg = socket.socket.sendmsg

    def sendmsg(self: socket.socket, buffers: Any, *args: Any) -> int:
        if len(args) >= 3 and args[2] is not None:  # (ancdata, flags, address)
            _check(args[2])
        elif self.type == socket.SOCK_DGRAM:
            try:
                _check(self.getpeername())  # a connected UDP socket's peer
            except OSError as exc:
                if isinstance(exc, NetworkBlocked):
                    raise
        return real_sendmsg(self, buffers, *args)

    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
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
