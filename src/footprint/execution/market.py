"""One book of live option quotes, shared by everyone who needs a price.

The runner publishes each quote once; trade management reads it from here, and
the simulated broker fills against the very same book. So a decision and the
simulated fill can never disagree about the market (review R12): there is no
second copy of a quote anywhere to fall out of step.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from footprint.execution.orders import Quote

NO_QUOTE_TS = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO = Decimal(0)


class QuoteBook:
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._listeners: list[Callable[[Quote], None]] = []

    def subscribe(self, listener: Callable[[Quote], None]) -> None:
        self._listeners.append(listener)

    def publish(self, quote: Quote) -> None:
        previous = self._quotes.get(quote.contract_id)
        if previous is not None and quote.ts < previous.ts:
            raise ValueError(f"quote for {quote.contract_id} is older than the one in the book")
        self._quotes[quote.contract_id] = quote
        for listener in self._listeners:
            listener(quote)

    def latest(self, contract_id: str) -> Quote | None:
        return self._quotes.get(contract_id)

    def latest_or_empty(self, contract_id: str) -> Quote:
        """The latest quote, or an empty, ancient one that every check treats as stale."""
        quote = self._quotes.get(contract_id)
        return quote if quote is not None else Quote(contract_id, _ZERO, _ZERO, NO_QUOTE_TS)
