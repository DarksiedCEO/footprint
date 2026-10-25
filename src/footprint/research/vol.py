"""A daily volatility index (Cboe's VXN_History.csv / VIX_History.csv) as the option's IV.

UNVERIFIED: the index level is used as the implied volatility of every option valued,
whatever its strike or expiry.

File format, as Cboe publishes it: header `DATE,OPEN,HIGH,LOW,CLOSE`, DATE as MM/DD/YYYY,
one row per date in strictly increasing order, values in percent (the divisor that turns
them into a fraction is config). Any bad row fails loudly with the file and line.

No lookahead: a trade on session day D uses the most recent CLOSE dated strictly before
D. If there is none, or it is more than `max_age_days` calendar days before D, the value
is missing (None): the trade is not valued, and the caller counts it as skipped.
"""

from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

HEADER = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")
_DATE_FORMAT = "%m/%d/%Y"


def vol_matches(path: Path, index: str) -> bool:
    """Whether a Cboe history file is that of `index` (research.yaml
    instruments.<profile>.vol_index): its file name's first part, up to the first "_"
    or ".", must be the index's name, case aside (Cboe names its files that way). The
    CSV content cannot tell one index from another (same header), so the name is the
    check; a file named otherwise is refused, never guessed (review L-4)."""
    first = path.name.split(".")[0].split("_")[0]
    return first.upper() == index.upper()


@dataclass(frozen=True, slots=True)
class VolIndex:
    dates: tuple[date, ...]
    closes: tuple[float, ...]  # in the index's own unit (percent)
    divisor: float
    max_age_days: int

    def lookup(self, day: date) -> float | None:
        """The volatility (a fraction) to use on session day `day`, or None (skip)."""
        at = bisect.bisect_left(self.dates, day) - 1
        if at < 0:
            return None
        if (day - self.dates[at]).days > self.max_age_days:
            return None
        return self.closes[at] / self.divisor


def load_vol(path: Path, divisor: float, max_age_days: int) -> VolIndex:
    dates: list[date] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = tuple(cell.strip().upper() for cell in next(reader, []))
        if header != HEADER:
            raise ValueError(f"{path}: header must be {','.join(HEADER)}, got {header}")
        for row in reader:
            where = f"{path}:{reader.line_num}"
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != len(HEADER):
                raise ValueError(f"{where}: expected {len(HEADER)} values, got {len(row)}")
            try:
                day = datetime.strptime(row[0].strip(), _DATE_FORMAT).replace(tzinfo=UTC).date()
                close = float(row[-1])
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"{where}: CLOSE must be a finite number > 0, got {row[-1]!r}")
            if dates and day <= dates[-1]:
                raise ValueError(f"{where}: date {day} is not after the previous {dates[-1]}")
            dates.append(day)
            closes.append(close)
    return VolIndex(tuple(dates), tuple(closes), divisor, max_age_days)
