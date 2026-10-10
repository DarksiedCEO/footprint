"""Q3 slice 2: the option-chain recorder, without ibapi.

    snapshot_rows(now, recorded) -> the rows `footprint.data.chain_record.write_snapshot`
                                    writes for one snapshot taken at `now`
    record_chain(market, directory, ...) -> the recording loop (exit code)

`recorded` is `MarketData.recorded_chain(now)` (the read-only MarketData, quotes no older
than the configured quote age, open interest None when IBKR sent none). Every row
carries the one future quote with its own time, and each option's own quote time, so
`write_snapshot` can refuse a stale or future-stamped quote of either kind. Nothing is
filled in: no recorded chain, no rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from footprint.brokers.ibkr.market import RecordedChain
from footprint.core.models import require_utc
from footprint.data.chain_record import ChainRow, write_snapshot


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


class ChainSource(Protocol):
    """What the recorder reads from MarketData."""

    def step(self, now: datetime) -> None: ...

    def recorded_chain(self, now: datetime) -> RecordedChain | None: ...


def record_chain(
    market: ChainSource,
    directory: Path,
    *,
    interval_seconds: int,
    run_seconds: int,
    max_age_seconds: int,
    poll_seconds: float,
    say: Callable[[str], None],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    """Record one snapshot every `interval_seconds` for `run_seconds`, stepping
    MarketData at least every `poll_seconds` in between.

    - A snapshot is read after the step, at a fresh clock reading, so no quote that
      arrived during the step is stamped after it.
    - Snapshots missed while a step was slow are skipped, never back-filled; none is
      taken at or after the end.
    - No chain (a disconnect, no fresh future or option quote): said, nothing written.
    - Rows that cannot be built or a snapshot `write_snapshot` refuses (a stale or
      future-stamped quote, a torn or foreign day file) or cannot write (OSError): an
      ERROR line naming the reason (the file, when it is the file), nothing written, and
      the recording goes on.

    Returns 0 when at least one snapshot was written and none was refused or failed;
    otherwise 1. Every number comes from config via the caller."""
    if interval_seconds <= 0 or run_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("record_chain: interval, run length and poll must be positive")
    if not directory.is_dir():
        say(f"REFUSED: {directory} is not a directory")
        return 1
    begun = clock()
    require_utc(begun, "clock()")
    end = begun + timedelta(seconds=run_seconds)
    interval = timedelta(seconds=interval_seconds)
    due = begun
    written = empty = refused = failed = 0
    while True:
        now = clock()
        if now >= end:
            break
        market.step(now)
        taken = clock()
        if due <= taken < end:
            try:
                rows = snapshot_rows(taken, market.recorded_chain(taken))
                if not rows:
                    empty += 1
                    say(f"no fresh chain at {taken.isoformat()}: nothing written")
                else:
                    path = write_snapshot(directory, rows, max_age_seconds)
                    written += 1
                    say(f"{taken.isoformat()}: {len(rows)} options written to {path}")
            except ValueError as exc:
                refused += 1
                say(f"ERROR: snapshot at {taken.isoformat()} refused: {exc}; nothing written")
            except OSError as exc:
                failed += 1
                say(f"ERROR: snapshot at {taken.isoformat()} not written: {exc}")
            while due <= taken:
                due += interval
        wake = min(due, end)
        now = clock()
        sleep(max(0.0, min(poll_seconds, (wake - now).total_seconds())))
    say(
        f"recording ended: {written} snapshots written, {empty} with no fresh chain, "
        f"{refused} refused, {failed} failed"
    )
    return 0 if written and not refused and not failed else 1
