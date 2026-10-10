"""Q3 slice 2: the option-chain recorder's read, without ibapi.

    snapshot_rows(now, recorded) -> the rows `footprint.data.chain_record.write_snapshot`
                                    writes for one snapshot taken at `now`

`recorded` is `MarketData.recorded_chain(now)` (the read-only MarketData, quotes no older
than the configured quote age, open interest None when IBKR sent none). Every row
carries the one future quote with its own time, and each option's own quote time, so
`write_snapshot` can refuse a stale or future-stamped quote of either kind. Nothing is
filled in: no recorded chain, no rows.
"""

from __future__ import annotations

from datetime import datetime

from footprint.brokers.ibkr.market import RecordedChain
from footprint.core.models import require_utc
from footprint.data.chain_record import ChainRow


def snapshot_rows(now: datetime, recorded: RecordedChain | None) -> list[ChainRow]:
    """One ChainRow per recorded option, all stamped `now`; [] for no chain."""
    require_utc(now, "now")
    if recorded is None:
        return []
    future = recorded.future
    return [
        ChainRow(
            ts=now,
            future_bid=future.bid,
            future_ask=future.ask,
            future_quote_ts=future.ts,
            contract_id=o.quote.contract_id,
            expiry=o.expiry,
            strike=o.strike,
            right=o.right,
            bid=o.quote.bid,
            ask=o.quote.ask,
            quote_ts=o.quote.ts,
            open_interest=o.open_interest,
        )
        for o in recorded.options
    ]
