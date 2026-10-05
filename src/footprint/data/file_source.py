"""File-backed data source: reads bars from a CSV file."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from footprint.core.models import Bar

COLUMNS = ("ts", "open", "high", "low", "close", "volume")


class CsvBarSource:
    """Yields one Bar per CSV row.

    Expects a header row with the columns in COLUMNS. `ts` must be ISO-8601 with
    an explicit offset; it is converted to UTC. Any bad row raises ValueError
    naming the file and line.
    """

    def __init__(self, path: Path, symbol: str) -> None:
        self._path = path
        self._symbol = symbol

    def bars(self) -> Iterator[Bar]:
        with self._path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if missing := [column for column in COLUMNS if column not in header]:
                raise ValueError(f"{self._path}: missing columns: {missing}")
            for row in reader:
                try:
                    yield self._parse(row)
                except (ValueError, TypeError, InvalidOperation) as exc:
                    raise ValueError(f"{self._path}:{reader.line_num}: {exc}") from exc

    def _parse(self, row: dict[str, str]) -> Bar:
        ts = datetime.fromisoformat(row["ts"])
        if ts.tzinfo is None:
            raise ValueError(f"ts must include a UTC offset, got {row['ts']!r}")
        return Bar(
            symbol=self._symbol,
            ts=ts.astimezone(UTC),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=int(row["volume"]),
        )
