"""Q3 slice 1: the option-chain recorder's file format, without ibapi.

    write_snapshot(directory, rows, max_age_seconds) -> the day file written to

One CSV per UTC day, `<directory>/chain-YYYY-MM-DD.csv`, header first, one line per
option quote of one snapshot (every row of a snapshot carries the same `ts`). It is the
raw material for calibrating D8 (minimum open interest) and D22 (the backtest's option
model), so it never holds a guess:

- A quote older than `max_age_seconds` (the configured quote age) at the snapshot, or
  stamped after it, refuses the WHOLE snapshot: nothing of it is written. The future's
  quote is held to the same rule and its age is written too (G30-2), so fresh option
  quotes are never paired with an old future price; every row of a snapshot carries the
  one future quote.
- Open interest IBKR did not send is a blank cell, never 0 (`None`); 0 is written as 0.
- An existing day file is appended to only when its header is exactly this one and it
  ends in a complete line: a file cut off mid-line (a crash during a write) is refused,
  never appended to, because the next snapshot would merge into the torn line (G30-1).
- Each snapshot is flushed and fsynced before the call returns.

The recorder command (a later slice) builds the rows from the read-only MarketData.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from footprint.core.models import require_utc
from footprint.core.options import Right

HEADER = (
    "ts",
    "future_bid",
    "future_ask",
    "future_quote_age_seconds",
    "contract_id",
    "expiry",
    "strike",
    "right",
    "bid",
    "ask",
    "quote_age_seconds",
    "open_interest",
)


def _price(value: Decimal, field: str, *, positive: bool) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"ChainRow.{field} must be a finite Decimal >= 0")
    if positive and value == 0:
        raise ValueError(f"ChainRow.{field} must be > 0")


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One option's quote in one snapshot, with the future's quote at the same moment."""

    ts: datetime  # when the snapshot was taken
    future_bid: Decimal
    future_ask: Decimal
    future_quote_ts: datetime  # when the future's quote last changed
    contract_id: str
    expiry: date
    strike: Decimal
    right: Right
    bid: Decimal  # 0: no bid
    ask: Decimal
    quote_ts: datetime  # when this option's quote last changed
    open_interest: int | None  # None: IBKR sent none

    def __post_init__(self) -> None:
        require_utc(self.ts, "ts")
        require_utc(self.quote_ts, "quote_ts")
        require_utc(self.future_quote_ts, "future_quote_ts")
        _price(self.future_bid, "future_bid", positive=True)
        _price(self.future_ask, "future_ask", positive=True)
        if self.future_ask < self.future_bid:
            raise ValueError("ChainRow: the future quote is crossed")
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("ChainRow.contract_id must be a non-empty string")
        if not isinstance(self.expiry, date) or isinstance(self.expiry, datetime):
            raise ValueError("ChainRow.expiry must be a date")
        _price(self.strike, "strike", positive=True)
        if not isinstance(self.right, Right):
            raise ValueError("ChainRow.right must be a Right")
        _price(self.bid, "bid", positive=False)
        _price(self.ask, "ask", positive=True)
        if self.ask < self.bid:
            raise ValueError(f"ChainRow {self.contract_id}: the quote is crossed")
        oi = self.open_interest
        if oi is not None and (isinstance(oi, bool) or not isinstance(oi, int) or oi < 0):
            raise ValueError("ChainRow.open_interest must be None or an int >= 0")


def day_file(directory: Path, ts: datetime) -> Path:
    """The file a snapshot taken at `ts` belongs in (its UTC day)."""
    require_utc(ts, "ts")
    return directory / f"chain-{ts.date().isoformat()}.csv"


def _seconds(age: timedelta) -> str:
    return f"{age.total_seconds():.3f}"


def _line(row: ChainRow) -> list[str]:
    return [
        row.ts.isoformat(),
        str(row.future_bid),
        str(row.future_ask),
        _seconds(row.ts - row.future_quote_ts),
        row.contract_id,
        row.expiry.isoformat(),
        str(row.strike),
        row.right.value,
        str(row.bid),
        str(row.ask),
        _seconds(row.ts - row.quote_ts),
        "" if row.open_interest is None else str(row.open_interest),
    ]


def write_snapshot(directory: Path, rows: Sequence[ChainRow], max_age_seconds: int) -> Path:
    """Append one snapshot to its day file (creating it with the header); every row is
    checked before anything is written. Raises ValueError (nothing written) on an empty
    or mixed snapshot, a stale or future-stamped quote (option or future), or a day file
    with another header or cut off mid-line; OSError from the file system as it comes."""
    if not rows:
        raise ValueError("a snapshot with no quotes is not written")
    first = rows[0]
    ts = first.ts
    future = (first.future_bid, first.future_ask, first.future_quote_ts)
    limit = timedelta(seconds=max_age_seconds)
    if first.future_quote_ts > ts:
        raise ValueError("the future's quote is stamped after the snapshot")
    if ts - first.future_quote_ts > limit:
        raise ValueError(
            f"the future's quote is older than {max_age_seconds} s; the snapshot is not written"
        )
    for row in rows:
        if row.ts != ts:
            raise ValueError("a snapshot's rows must all be taken at one moment")
        if (row.future_bid, row.future_ask, row.future_quote_ts) != future:
            raise ValueError("a snapshot's rows must all carry one future quote")
        if row.quote_ts > ts:
            raise ValueError(f"{row.contract_id}: its quote is stamped after the snapshot")
        if ts - row.quote_ts > limit:
            raise ValueError(
                f"{row.contract_id}: its quote is older than {max_age_seconds} s; "
                "the snapshot is not written"
            )
    path = day_file(directory, ts)
    header = list(HEADER)
    fresh = not path.exists() or path.stat().st_size == 0
    if not fresh:
        with path.open(newline="", encoding="utf-8") as handle:
            found = next(csv.reader(handle), None)
        if found != header:
            raise ValueError(f"{path} has another header: it is not appended to")
        with path.open("rb") as raw:
            raw.seek(-1, os.SEEK_END)
            if raw.read(1) != b"\n":
                raise ValueError(
                    f"{path} ends mid-line (a write was cut off): it is not appended to; "
                    "its last snapshot may be incomplete"
                )
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if fresh:
            writer.writerow(header)
        writer.writerows(_line(row) for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    return path
