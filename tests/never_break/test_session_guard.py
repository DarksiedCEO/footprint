"""Session guard: windows by Pacific clock (DST-aware), trading days, overnight flat rule."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from footprint.core.config import InstrumentProfile
from footprint.decision.session import (
    NewsBlackout,
    SessionKind,
    overnight_must_be_flat,
    session_at,
)


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 6, 11, 0, tzinfo=UTC), SessionKind.PRIME),  # Tue 04:00 PDT
        (datetime(2026, 10, 6, 21, 0, tzinfo=UTC), SessionKind.PRIME),  # Tue 14:00 PDT
        (datetime(2026, 10, 6, 21, 1, tzinfo=UTC), SessionKind.CLOSED),  # Tue 14:01 PDT
        (datetime(2026, 10, 6, 22, 0, tzinfo=UTC), SessionKind.OVERNIGHT),  # Tue 15:00 PDT
        (datetime(2026, 10, 7, 10, 59, tzinfo=UTC), SessionKind.OVERNIGHT),  # Wed 03:59 PDT
        (datetime(2026, 10, 11, 22, 0, tzinfo=UTC), SessionKind.OVERNIGHT),  # Sun 15:00 PDT
        (datetime(2026, 10, 12, 8, 0, tzinfo=UTC), SessionKind.OVERNIGHT),  # Mon 01:00 PDT
        (datetime(2026, 10, 10, 8, 0, tzinfo=UTC), SessionKind.CLOSED),  # Sat 01:00 PDT
        (datetime(2026, 1, 6, 14, 30, tzinfo=UTC), SessionKind.PRIME),  # Tue 06:30 PST
        (datetime(2026, 1, 6, 11, 30, tzinfo=UTC), SessionKind.OVERNIGHT),  # Tue 03:30 PST
    ],
)
def test_session_windows(
    instrument: InstrumentProfile, utc: datetime, expected: SessionKind
) -> None:
    assert session_at(utc, instrument) is expected


def test_dst_switch_nights_keep_the_flat_minute_at_0359_local(
    instrument: InstrumentProfile,
) -> None:
    """NB-07 across both DST switches: the forced-flat minute is 03:59 Pacific."""
    pacific = ZoneInfo("America/Los_Angeles")
    for flat_minute in (
        datetime(2026, 11, 2, 11, 59, tzinfo=UTC),  # Mon after fall-back: 03:59 PST
        datetime(2026, 3, 9, 10, 59, tzinfo=UTC),  # Mon after spring-forward: 03:59 PDT
    ):
        assert flat_minute.astimezone(pacific).strftime("%H:%M") == "03:59"
        assert overnight_must_be_flat(flat_minute, instrument)
    # Fall-back night is Sunday morning: both 01:30s (PDT, then PST) are a closed weekend.
    assert session_at(datetime(2026, 11, 1, 8, 30, tzinfo=UTC), instrument) is SessionKind.CLOSED
    assert session_at(datetime(2026, 11, 1, 9, 30, tzinfo=UTC), instrument) is SessionKind.CLOSED
    # Sunday 15:00 PST opens Monday's overnight session.
    assert session_at(datetime(2026, 11, 1, 23, 0, tzinfo=UTC), instrument) is SessionKind.OVERNIGHT


def test_nb07_overnight_flat_by_session_end(instrument: InstrumentProfile) -> None:
    """NB-07: an overnight position must be flat at the overnight session's last minute."""
    assert not overnight_must_be_flat(datetime(2026, 10, 7, 10, 58, 59, tzinfo=UTC), instrument)
    assert overnight_must_be_flat(datetime(2026, 10, 7, 10, 59, tzinfo=UTC), instrument)  # 03:59
    assert overnight_must_be_flat(datetime(2026, 10, 7, 11, 0, tzinfo=UTC), instrument)  # 04:00
    assert overnight_must_be_flat(datetime(2026, 10, 10, 8, 0, tzinfo=UTC), instrument)  # Sat


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 6, 13, 30),  # noqa: DTZ001 - naive on purpose
        datetime(2026, 1, 6, 13, 30, tzinfo=ZoneInfo("Europe/London")),  # offset 0 but not UTC
    ],
)
def test_session_guard_accepts_only_utc(instrument: InstrumentProfile, bad: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        session_at(bad, instrument)
    with pytest.raises(ValueError, match="UTC"):
        NewsBlackout("CPI", start=bad, end=bad)


def test_news_blackout_rejects_inverted_window() -> None:
    start = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="end"):
        NewsBlackout("CPI", start=start, end=start.replace(minute=0, hour=11))
