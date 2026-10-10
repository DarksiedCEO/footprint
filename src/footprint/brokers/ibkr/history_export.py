"""Q2: the `history` export, without ibapi (the command in `history.py` wires it to IB
Gateway through the read-only gateway).

    export_history(market, out, window_from, instrument, wait_seconds, max_missing, ...)

- Refuses at once, before any time is spent loading, if `out` or `<out>.partial`
  exists (never overwrites; a failed run's partial is looked at by a person first).
- Steps MarketData (the same service the runner reads: front future, spliced and
  back-adjusted, Saturdays skipped, requests paced from config) until its `bars` are
  ready, i.e. the whole history has loaded and a first poll has answered. It looks at
  least once, and once more at the deadline itself.
- Refuses a history with a gap (`footprint.data.gaps.first_gap`), from
  `window_from(now)` to the last minute closed when the bars were read, both taken at
  that same `now`: MarketData's window moves at 00:00 UTC, so a start taken before a
  long load would be a day off (G29-1), and a missing tail is a gap too (G29-2).
- Only then writes the CSV `CsvBarSource` reads (`write_bars`: `.partial` first, linked
  to the finished name only when every bar is on disk).

Returns 0 when the file was written; 1 (nothing under the finished name) on a timeout,
a gap or a write failure, saying which. Every number comes from config via the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from footprint.core.config import InstrumentProfile, Profiles
from footprint.core.models import Bar
from footprint.data.csv_export import ExportRefused, partial_path, write_bars
from footprint.data.gaps import first_gap


def with_days(profiles: Profiles, days: int | None) -> Profiles:
    """`--days N`: the same profiles with `market_data.history_days` set to N for this
    export only (config is not changed); None keeps the configured length."""
    if days is None:
        return profiles
    if days < 1:
        raise ValueError(f"--days {days}: at least one day of history")
    market_data = replace(profiles.execution.market_data, history_days=days)
    return replace(profiles, execution=replace(profiles.execution, market_data=market_data))


def window_start(now: datetime, history_days: int) -> datetime:
    """Where MarketData's history begins: 00:00 UTC, `history_days` days before today."""
    return datetime.combine(now.date() - timedelta(days=history_days), time(0), tzinfo=UTC)


class HistorySource(Protocol):
    """What the export reads from MarketData."""

    def step(self, now: datetime) -> None: ...

    def bars(self, now: datetime) -> Sequence[Bar]: ...


def export_history(
    market: HistorySource,
    out: Path,
    window_from: Callable[[datetime], datetime],
    instrument: InstrumentProfile,
    wait_seconds: float,
    max_missing: int,
    poll_seconds: float,
    say: Callable[[str], None],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    partial = partial_path(out)
    if out.exists():
        say(f"REFUSED: {out} already exists: it is never overwritten")
        return 1
    if partial.exists():
        say(f"REFUSED: {partial} exists (an earlier export failed): look at it, delete it")
        return 1
    begun = clock()
    while True:
        now = clock()
        market.step(now)
        bars = market.bars(now)
        waited = (now - begun).total_seconds()
        if bars or waited >= wait_seconds:
            break
        sleep(min(poll_seconds, wait_seconds - waited))
    if not bars:
        say(
            f"NO HISTORY after {wait_seconds:g} s: MarketData has not finished loading it "
            "(IBKR's reason, if it sent one, is in the ALARM lines above); nothing written"
        )
        return 1
    gap = first_gap(bars, window_from(now), instrument, max_missing, until=now)
    if gap is not None:
        say(f"REFUSED: the history has a gap: {gap}; nothing written")
        return 1
    try:
        count = write_bars(bars, out)
    except (ExportRefused, ValueError, OSError) as exc:
        say(f"EXPORT FAILED: {exc} (anything written is in {partial})")
        return 1
    say(f"wrote {count} bars, {bars[0].ts.isoformat()} to {bars[-1].ts.isoformat()}, to {out}")
    return 0
