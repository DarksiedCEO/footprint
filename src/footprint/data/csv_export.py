"""Writes minute bars in exactly the CSV format `CsvBarSource` reads (Q2, history export).

    write_bars(bars, Path("data/nq_1m.csv"))

- Never overwrites: an existing file at the path, or a `.partial` file left by an
  earlier failed export, refuses the export (ExportRefused) and touches neither.
- Never looks complete when it is not: the bars go to `<name>.partial` first; only
  after the last bar is written and flushed to disk is it linked to the real name (a
  link, not a rename, so a file created there meanwhile is never replaced). On any
  failure (the bars' source raising, bars out of order, two symbols, no bars at all)
  the error is raised and the file stays named `.partial`.
- The checks are the reader's own: one symbol, strictly increasing UTC timestamps.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from footprint.core.models import Bar
from footprint.data.file_source import COLUMNS

PARTIAL_SUFFIX = ".partial"


class ExportRefused(RuntimeError):
    """The export would overwrite a file (the finished one, or a failed run's partial)."""


def partial_path(path: Path) -> Path:
    return path.with_name(path.name + PARTIAL_SUFFIX)


def write_bars(bars: Iterable[Bar], path: Path) -> int:
    """Write `bars` (oldest first) to `path`; return how many were written."""
    partial = partial_path(path)
    if path.exists():
        raise ExportRefused(f"{path} already exists: it is never overwritten")
    try:
        handle = partial.open("x", newline="", encoding="utf-8")
    except FileExistsError:
        raise ExportRefused(
            f"{partial} exists (an earlier export failed): look at it, delete it, run again"
        ) from None
    count = 0
    with handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        symbol: str | None = None
        previous: datetime | None = None
        for bar in bars:
            if symbol is None:
                symbol = bar.symbol
            elif bar.symbol != symbol:
                raise ValueError(f"bar at {bar.ts}: symbol {bar.symbol!r}, not {symbol!r}")
            if previous is not None and bar.ts <= previous:
                raise ValueError(f"bar ts {bar.ts} is not later than the previous bar's {previous}")
            previous = bar.ts
            writer.writerow(
                (bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume)
            )
            count += 1
        if count == 0:
            raise ValueError("no bars to export: an empty history is a failure")
        handle.flush()
        os.fsync(handle.fileno())
    os.link(partial, path)  # FileExistsError if the name appeared meanwhile: never replaced
    partial.unlink()
    return count
