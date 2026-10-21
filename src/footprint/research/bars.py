"""Continuous 1-minute futures bars, grouped by trading session day, as plain floats.

Rows are read by `footprint.data.file_source.CsvBarSource`, so every rule of that reader
holds here unchanged: the header must name ts,open,high,low,close,volume; `ts` is ISO-8601
with an explicit offset; timestamps strictly increase; a bad row (a missing value, a
price <= 0, a high below the low, ...) fails loudly with the file and line.

A bar's `ts` is the moment it OPENS (the codebase's convention). Its session day is the
New York calendar date of its wall-clock time moved forward by (midnight - roll): a bar
opening at or after the roll belongs to the next calendar day's session, one opening
before it to its own date. The timezone and the roll are config (research.yaml).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from footprint.core.models import Bar
from footprint.data.file_source import CsvBarSource

_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Day:
    """One session day's bars, in time order. `ts` is UTC (bar open); `clock` is the bar's
    opening wall-clock time in the research timezone."""

    day: date
    ts: tuple[datetime, ...]
    clock: tuple[time, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.ts)


def session_day(ts: datetime, zone: ZoneInfo, roll: time) -> date:
    """The session day of a bar opening at `ts` (UTC)."""
    if ts.tzinfo is None:
        raise ValueError(f"ts must be timezone-aware, got {ts!r}")
    wall = ts.astimezone(zone).replace(tzinfo=None)
    shift = _DAY - timedelta(hours=roll.hour, minutes=roll.minute)
    return (wall + shift).date()


def group_days(bars: Iterable[Bar], zone: ZoneInfo, roll: time) -> list[Day]:
    """Bars (strictly increasing ts, as CsvBarSource yields them) grouped by session day."""
    days: list[Day] = []
    rows: list[Bar] = []
    current: date | None = None
    previous: datetime | None = None
    for bar in bars:
        if previous is not None and bar.ts <= previous:
            raise ValueError(f"bars out of order: {bar.ts} after {previous}")
        previous = bar.ts
        day = session_day(bar.ts, zone, roll)
        if current is not None and day != current:
            days.append(_day(current, rows, zone))
            rows = []
        current = day
        rows.append(bar)
    if current is not None:
        days.append(_day(current, rows, zone))
    return days


def _day(day: date, rows: list[Bar], zone: ZoneInfo) -> Day:
    return Day(
        day=day,
        ts=tuple(bar.ts.astimezone(UTC) for bar in rows),
        clock=tuple(bar.ts.astimezone(zone).time() for bar in rows),
        open=tuple(float(bar.open) for bar in rows),
        high=tuple(float(bar.high) for bar in rows),
        low=tuple(float(bar.low) for bar in rows),
        close=tuple(float(bar.close) for bar in rows),
        volume=tuple(bar.volume for bar in rows),
    )


def load_days(path: Path, symbol: str, zone: ZoneInfo, roll: time) -> list[Day]:
    """Read a 1-minute CSV and group it by session day. Fails loudly on any bad row."""
    return group_days(CsvBarSource(path, symbol).bars(), zone, roll)
