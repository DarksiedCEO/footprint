"""A read-only first look at the account through IB Gateway.

    uv run python -m footprint.brokers.ibkr.check

Connects with the settings in .env, waits for the full resync, prints what the system
would see (the account, its positions and open orders in the system's instrument, and
how many orders on anything else are open), then disconnects. It cannot send, change
or cancel an order: the gateway it hands the adapter refuses all three.

Exit code 0 when the resync completed, 1 when it did not (or the account was refused).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from footprint.brokers.ibkr.adapter import (
    BrokerUnavailable,
    ContractBook,
    IbkrBroker,
    IbkrSettings,
    PositionsInDoubt,
)
from footprint.brokers.ibkr.binding import IbBinding, IbConnectionSettings, read_env_file
from footprint.brokers.ibkr.gateway import IbGateway, IbOrderRequest
from footprint.core.config import Profiles, load_profiles

_POLL_SECONDS = 0.1


class ReadOnlyGateway:
    """The binding, with every order-changing call refused."""

    def __init__(self, inner: IbGateway) -> None:
        self._inner = inner

    def place_order(self, request: IbOrderRequest) -> None:
        raise PermissionError("read-only check: no order is sent")

    def cancel_order(self, ib_id: int) -> None:
        raise PermissionError("read-only check: no order is cancelled")

    def global_cancel(self) -> None:
        raise PermissionError("read-only check: no order is cancelled")

    def request_open_orders(self) -> None:
        self._inner.request_open_orders()

    def request_executions(self) -> None:
        self._inner.request_executions()

    def request_positions(self) -> None:
        self._inner.request_positions()


def check(
    profiles: Profiles,
    settings: IbConnectionSettings,
    wait_seconds: float,
    out: TextIO,
    binding_factory: Callable[[IbConnectionSettings, Callable[[str], None]], IbBinding] = (
        lambda s, alarm: IbBinding(s, alarm)
    ),
) -> int:
    def say(message: str) -> None:
        print(message, file=out, flush=True)

    binding = binding_factory(settings, lambda message: say(f"ALARM: {message}"))
    scope = (profiles.instrument.symbol, profiles.execution.ibkr.option_security_type)
    adapter = IbkrBroker(
        ReadOnlyGateway(binding),
        ContractBook(),
        IbkrSettings.from_config(profiles.execution.ibkr),
        scope,
        on_alarm=lambda message: say(f"ALARM: {message}"),
    )
    binding.attach(adapter)
    stop = threading.Event()
    worker = threading.Thread(target=binding.run, args=(stop,), name="ibkr", daemon=True)
    say(f"connecting to IB Gateway at {settings.host}:{settings.port} for {settings.account}")
    worker.start()
    deadline = time.monotonic() + wait_seconds
    try:
        while not adapter.ready and binding.refused is None and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
        if binding.refused is not None:
            say(f"REFUSED: {binding.refused}")
            return 1
        if not adapter.ready:
            say(f"NOT READY after {wait_seconds:g} s: is IB Gateway running and signed in?")
            return 1
        say(f"resynced. account {settings.account}; system scope {scope[0]} {scope[1]}")
        try:
            held = adapter.positions()
            say(f"positions in scope: {held or 'none'}")
        except PositionsInDoubt as exc:
            say(f"positions in scope: IN DOUBT ({exc})")
        orders = adapter.open_orders()
        say(f"open orders in scope: {len(orders)}")
        for order in orders:
            say(
                f"  {order.order_id}: {order.side.value} {order.quantity} {order.contract_id} "
                f"{order.order_type.value} limit {order.limit_price} stop {order.stop_price}"
            )
        say(f"open orders on anything else (never touched): {adapter.outside_open_orders}")
        return 0
    except BrokerUnavailable as exc:
        say(f"lost IB Gateway during the check: {exc}")
        return 1
    finally:
        stop.set()
        binding.stop()
        worker.join(timeout=wait_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m footprint.brokers.ibkr.check")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--instrument", default="nq.yaml")
    parser.add_argument(
        "--wait",
        type=float,
        default=None,
        help="seconds to wait for the resync (default: one reconnect plus the resync limit)",
    )
    args = parser.parse_args(argv)
    profiles = load_profiles(args.config, args.instrument)
    ibkr = profiles.execution.ibkr
    settings = IbConnectionSettings.from_env(read_env_file(str(args.env)), ibkr)
    wait = args.wait
    if wait is None:
        wait = float(ibkr.reconnect_seconds + ibkr.positions_disagree_alarm_seconds)
    return check(profiles, settings, wait, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
