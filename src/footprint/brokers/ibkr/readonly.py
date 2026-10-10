"""The read-only tools' gateway and the market-data probe (`check --market-data`).

ReadOnlyGateway is the binding with every order-changing call refused: the read-only
check, the probe and the data tools hand it to the adapter and to MarketData, so no
order can be placed, modified or cancelled through them. Market data requests (lookups,
quote streams, history) pass through unchanged; none of them touches an order.

The probe drives MarketData (its own `step`, the same service the runner reads) until
the traded future and at least one option on it have fresh quotes, or the configured
wait runs out. It prints the future's bid and ask and the option quotes nearest the
future, each with its quote age, and returns 0; or it names what is missing and
returns 1. Every price it prints is read from MarketData's fresh quote at that moment:
a quote that went stale is said to be stale, never printed. IBKR's own reason for
missing data (e.g. 354 no subscription, 10167 delayed only) reaches the operator
through MarketData's alarm, which the check prints as an ALARM line.

It prints the future's bid and ask, not its last trade: MarketData streams top of book
only (QuoteField has no last price), and the probe adds no stream of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from footprint.brokers.ibkr.gateway import ContractQuery, IbContract, IbOrderRequest
from footprint.core.options import OptionChain, OptionQuote
from footprint.execution.orders import Quote


class _ReadGateway(Protocol):
    """What the read-only gateway forwards: the binding's reads and market requests."""

    def request_open_orders(self) -> None: ...

    def request_executions(self) -> None: ...

    def request_positions(self) -> None: ...

    def request_contract_details(self, req_id: int, query: ContractQuery) -> None: ...

    def request_option_params(
        self,
        req_id: int,
        symbol: str,
        option_exchange: str,
        underlying_sec_type: str,
        underlying_con_id: int,
    ) -> None: ...

    def request_quotes(self, req_id: int, contract: IbContract) -> None: ...

    def cancel_quotes(self, req_id: int) -> None: ...

    def request_bars(
        self, req_id: int, query: ContractQuery, end: str, duration: str, keep_up_to_date: bool
    ) -> None: ...

    def cancel_bars(self, req_id: int) -> None: ...


class ReadOnlyGateway:
    """The binding, with every order-changing call refused."""

    def __init__(self, inner: _ReadGateway) -> None:
        self._inner = inner

    # --- refused: nothing here may place, modify or cancel an order ---------------------

    def place_order(self, request: IbOrderRequest) -> None:
        raise PermissionError("read-only check: no order is sent")

    def cancel_order(self, ib_id: int) -> None:
        raise PermissionError("read-only check: no order is cancelled")

    def global_cancel(self) -> None:
        raise PermissionError("read-only check: no order is cancelled")

    # --- account reads (the resync) ------------------------------------------------------

    def request_open_orders(self) -> None:
        self._inner.request_open_orders()

    def request_executions(self) -> None:
        self._inner.request_executions()

    def request_positions(self) -> None:
        self._inner.request_positions()

    # --- market data requests --------------------------------------------------------------

    def request_contract_details(self, req_id: int, query: ContractQuery) -> None:
        self._inner.request_contract_details(req_id, query)

    def request_option_params(
        self,
        req_id: int,
        symbol: str,
        option_exchange: str,
        underlying_sec_type: str,
        underlying_con_id: int,
    ) -> None:
        self._inner.request_option_params(
            req_id, symbol, option_exchange, underlying_sec_type, underlying_con_id
        )

    def request_quotes(self, req_id: int, contract: IbContract) -> None:
        self._inner.request_quotes(req_id, contract)

    def cancel_quotes(self, req_id: int) -> None:
        self._inner.cancel_quotes(req_id)

    def request_bars(
        self, req_id: int, query: ContractQuery, end: str, duration: str, keep_up_to_date: bool
    ) -> None:
        self._inner.request_bars(req_id, query, end, duration, keep_up_to_date)

    def cancel_bars(self, req_id: int) -> None:
        self._inner.cancel_bars(req_id)


class ProbeSource(Protocol):
    """The part of MarketData the probe reads."""

    def step(self, now: datetime) -> None: ...

    def chain(self, now: datetime) -> OptionChain | None: ...

    def underlying_quote(self, now: datetime) -> Quote | None: ...

    def option_quote(self, contract_id: str, now: datetime) -> Quote | None: ...


def _age(now: datetime, quote: Quote) -> str:
    return f"quote age {(now - quote.ts).total_seconds():.1f} s"


def _nearest(chain: OptionChain) -> list[OptionQuote]:
    price: Decimal = chain.underlying_price
    return sorted(
        chain.quotes,
        key=lambda q: (abs(q.strike - price), q.strike, q.right.value, q.contract_id),
    )


def probe_market(
    market: ProbeSource,
    wait_seconds: float,
    option_count: int,
    poll_seconds: float,
    say: Callable[[str], None],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """Step MarketData until the future and an option on it have fresh quotes, then print
    them (0); or, once `wait_seconds` has passed, name what is missing (1). It looks at
    least once, and once more at the deadline itself."""
    start = clock()
    while True:
        now = clock()
        market.step(now)
        future = market.underlying_quote(now)
        chain = market.chain(now)  # None without a fresh future quote, too
        waited = (now - start).total_seconds()
        if (future is not None and chain is not None) or waited >= wait_seconds:
            break
        sleep(min(poll_seconds, wait_seconds - waited))
    missing: list[str] = []
    if future is None:
        missing.append("no fresh future quote")
    else:
        say(f"future {future.contract_id}: bid {future.bid} ask {future.ask}, {_age(now, future)}")
    if chain is None:
        missing.append("no fresh option quote")
    else:
        shown = _nearest(chain)[:option_count]
        say(f"option quotes ({len(shown)} of {len(chain.quotes)} fresh, nearest the future first):")
        for q in shown:
            name = f"  {q.expiry.isoformat()} {q.strike} {q.right.value} {q.contract_id}"
            fresh = market.option_quote(q.contract_id, now)
            if fresh is None:
                say(f"{name}: no fresh quote now")
            else:
                say(f"{name}: bid {fresh.bid} ask {fresh.ask}, {_age(now, fresh)}")
    if missing:
        say(
            f"NO MARKET DATA after {wait_seconds:g} s: {'; '.join(missing)} "
            "(IBKR's reason, if it sent one, is in the ALARM lines above)"
        )
        return 1
    return 0
