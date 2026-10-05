"""G12: agents see completed bars only. G11: daily bars are cash-session bars."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.core.config import InstrumentProfile
from footprint.core.models import Bar
from footprint.data.aggregate import completed, resample, session_days
from tests.agents.conftest import bar

PT = ZoneInfo("America/Los_Angeles")
MIN = timedelta(minutes=1)


def pt(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """A moment on 2026-09-<day> Pacific, as UTC."""
    return datetime(2026, 9, day, hour, minute, second, tzinfo=PT).astimezone(UTC)


def minutes(start: datetime, count: int, base: float = 100.0) -> list[Bar]:
    return [
        bar(start + MIN * i, base + i, base + i + 1, base + i - 1, base + i) for i in range(count)
    ]


def test_completed_drops_the_bar_still_forming() -> None:
    bars = minutes(pt(1, 6, 30), 3)  # 06:30, 06:31, 06:32
    assert completed(bars, pt(1, 6, 32, 59), 1) == bars[:2]
    assert completed(bars, pt(1, 6, 33), 1) == bars


def test_completed_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        completed([], datetime(2026, 9, 1, 6, 30, tzinfo=PT), 1)


def test_resample_emits_only_closed_buckets_with_correct_ohlcv() -> None:
    bars = minutes(pt(1, 6, 30), 20)  # 06:30 .. 06:49
    assert resample(bars, 15, pt(1, 6, 44, 59)) == []  # 06:30 bucket still open
    out = resample(bars, 15, pt(1, 6, 50))
    assert len(out) == 1  # the 06:45 bucket is still forming
    first = out[0]
    assert first.ts == pt(1, 6, 30)
    assert first.open == bars[0].open
    assert first.close == bars[14].close
    assert first.high == max(b.high for b in bars[:15])
    assert first.low == min(b.low for b in bars[:15])
    assert first.volume == sum(b.volume for b in bars[:15])


def test_resample_aligns_buckets_to_the_clock() -> None:
    bars = minutes(pt(1, 6, 37), 10)  # 06:37 .. 06:46
    out = resample(bars, 15, pt(1, 7, 0))
    assert [b.ts for b in out] == [pt(1, 6, 30), pt(1, 6, 45)]
    assert out[0].volume == 8 * 100  # 06:37..06:44


def test_resample_rejects_bad_input() -> None:
    a = bar(pt(1, 6, 30), 1, 2, 1, 2)
    with pytest.raises(ValueError, match="increasing"):
        resample([a, a], 15, pt(1, 7, 0))
    with pytest.raises(ValueError, match="mixes symbols"):
        resample([a, bar(pt(1, 6, 31), 1, 2, 1, 2, symbol="ES")], 15, pt(1, 7, 0))


def test_session_days_use_only_cash_minutes(instrument: InstrumentProfile) -> None:
    cash = instrument.cash_session
    edges = [
        bar(pt(1, 6, 29), 50, 500, 40, 50),  # before the open: excluded
        bar(pt(1, 6, 30), 100, 110, 90, 105),
        bar(pt(1, 12, 59), 105, 120, 95, 115),
        bar(pt(1, 13, 0), 115, 900, 1, 115),  # opens at the close: excluded
    ]
    out = session_days(edges, cash, pt(2, 5, 0))
    assert out.today is None
    (day,) = out.days
    assert (day.open, day.high, day.low, day.close) == (
        Decimal(100),
        Decimal(120),
        Decimal(90),
        Decimal(115),
    )
    assert day.volume == 200


def test_session_days_today_in_progress_until_the_close(instrument: InstrumentProfile) -> None:
    cash = instrument.cash_session
    bars = minutes(pt(1, 6, 30), 5) + minutes(pt(2, 6, 30), 5)
    during = session_days(bars, cash, pt(2, 10, 0))
    assert len(during.days) == 1
    assert during.today is not None
    assert during.today.ts == pt(2, 6, 30)
    at_close = session_days(bars, cash, pt(2, 13, 0))
    assert at_close.today is None
    assert len(at_close.days) == 2
    before_open = session_days(bars, cash, pt(2, 6, 30))
    assert before_open.today is None  # no completed cash bar today yet
    assert [d.ts for d in before_open.days] == [pt(1, 6, 30)]
