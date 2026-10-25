from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from footprint.research.bars import Day
from footprint.research.settings import ResearchSettings, load_research
from tests.conftest import REPO_ROOT

NY = ZoneInfo("America/New_York")
MINUTE = timedelta(minutes=1)


@pytest.fixture(scope="session")
def research() -> ResearchSettings:
    return load_research(REPO_ROOT / "config", "nq.yaml")


def make_day(
    start: datetime,
    closes: Sequence[float],
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    opens: Sequence[float] | None = None,
    day: date | None = None,
) -> Day:
    """One bar per close, a minute apart from `start` (New York wall time, aware)."""
    n = len(closes)
    opens = list(opens) if opens is not None else [closes[0], *closes[:-1]]
    highs = (
        list(highs)
        if highs is not None
        else [max(o, c) for o, c in zip(opens, closes, strict=True)]
    )
    lows = (
        list(lows) if lows is not None else [min(o, c) for o, c in zip(opens, closes, strict=True)]
    )
    stamps = [(start + k * MINUTE).astimezone(UTC) for k in range(n)]
    return Day(
        day=day or start.date(),
        ts=tuple(stamps),
        clock=tuple(s.astimezone(NY).time() for s in stamps),
        open=tuple(float(x) for x in opens),
        high=tuple(float(x) for x in highs),
        low=tuple(float(x) for x in lows),
        close=tuple(float(x) for x in closes),
        volume=tuple(1 for _ in range(n)),
    )


def ny(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime.combine(date(y, m, d), time(hh, mm), tzinfo=NY)


def write_bars(tmp_path: Path, rows: Sequence[str]) -> Path:
    path = tmp_path / "bars.csv"
    path.write_text("ts,open,high,low,close,volume\n" + "".join(r + "\n" for r in rows))
    return path
