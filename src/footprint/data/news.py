"""News blackouts from a CSV (columns name,start,end; ISO-8601 with offsets)."""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from footprint.decision.session import NewsBlackout


def blackouts_from(path: Path | None) -> Callable[[datetime], Iterable[NewsBlackout]]:
    """The windows in `path` (none if `path` is None). A time without an offset is
    refused: a blackout must never shift by the machine's time zone."""
    if path is None:
        return lambda now: ()
    with path.open(newline="", encoding="utf-8") as handle:
        windows = tuple(
            NewsBlackout(
                row["name"],
                _utc(row["start"], path),
                _utc(row["end"], path),
            )
            for row in csv.DictReader(handle)
        )

    return lambda now: windows  # the system checks each window against the time itself


def _utc(text: str, path: Path) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"{path}: {text!r} has no UTC offset")
    return moment.astimezone(UTC)
