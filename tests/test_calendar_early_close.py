"""The exchange calendar: early closes end the prime session, the cutoff before them,
the short cash session, and the warning before the calendar runs out (D34)."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from footprint.core.calendar import (
    SessionKind,
    calendar_warning,
    cash_close,
    entry_cutoff,
    prime_close,
    session_at,
    trading_day_at,
)
from footprint.core.config import InstrumentProfile

PT = ZoneInfo("America/Los_Angeles")


def pt(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), PT).astimezone(UTC)


FRI_AFTER_THANKSGIVING = date(2026, 11, 27)
THANKSGIVING = date(2026, 11, 26)
CHRISTMAS_EVE = date(2026, 12, 24)
NORMAL = date(2026, 11, 24)


def test_the_shipped_calendar_has_the_year_end_dates(instrument: InstrumentProfile) -> None:
    assert date(2026, 12, 25) in instrument.holidays
    assert date(2027, 1, 1) in instrument.holidays
    for day in (THANKSGIVING, FRI_AFTER_THANKSGIVING, CHRISTMAS_EVE):
        assert instrument.early_closes[day] == time(10, 0)
    assert date(2026, 12, 31) not in instrument.early_closes  # a normal day for equities


def test_the_prime_session_ends_at_the_early_close(instrument: InstrumentProfile) -> None:
    day = FRI_AFTER_THANKSGIVING
    assert session_at(pt(day, 9, 59), instrument) is SessionKind.PRIME
    assert session_at(pt(day, 10, 0), instrument) is SessionKind.PRIME  # its last minute
    assert session_at(pt(day, 10, 1), instrument) is SessionKind.CLOSED
    assert trading_day_at(pt(day, 10, 1), instrument) is None
    assert session_at(pt(NORMAL, 13, 59), instrument) is SessionKind.PRIME  # untouched


def test_the_evening_after_an_early_close_leads_into_the_next_day(
    instrument: InstrumentProfile,
) -> None:
    assert session_at(pt(THANKSGIVING, 15, 30), instrument) is SessionKind.OVERNIGHT
    assert trading_day_at(pt(THANKSGIVING, 15, 30), instrument) == FRI_AFTER_THANKSGIVING
    # Christmas Eve's evening leads into Christmas: closed.
    assert session_at(pt(CHRISTMAS_EVE, 19, 0), instrument) is SessionKind.CLOSED


def test_the_cutoff_is_the_lead_before_the_days_own_close(instrument: InstrumentProfile) -> None:
    early = entry_cutoff(pt(FRI_AFTER_THANKSGIVING, 8, 0), instrument, 5)
    assert early == pt(FRI_AFTER_THANKSGIVING, 9, 55)
    assert entry_cutoff(pt(NORMAL, 8, 0), instrument, 5) == pt(NORMAL, 13, 55)
    assert prime_close(FRI_AFTER_THANKSGIVING, instrument) == time(10, 0)
    assert prime_close(NORMAL, instrument) == instrument.prime_session.end


def test_the_cash_session_is_cut_short_by_an_early_close(instrument: InstrumentProfile) -> None:
    assert cash_close(FRI_AFTER_THANKSGIVING, instrument) == time(10, 0)
    assert cash_close(NORMAL, instrument) == instrument.cash_session.end
    late = replace(instrument, early_closes={NORMAL: time(13, 30)})  # after the cash close
    assert cash_close(NORMAL, late) == instrument.cash_session.end


def test_the_calendar_warns_before_it_runs_out(instrument: InstrumentProfile) -> None:
    through = instrument.calendar_through
    warn = instrument.calendar_warn_days
    assert calendar_warning(through - timedelta(days=warn + 1), instrument) is None
    soon = calendar_warning(through - timedelta(days=warn), instrument)
    assert soon is not None
    assert f"({warn} days)" in soon
    assert "CME" not in soon  # instrument facts stay in the profile
    expired = calendar_warning(through + timedelta(days=1), instrument)
    assert expired is not None
    assert expired.startswith("EXCHANGE CALENDAR EXPIRED")
    assert calendar_warning(through, instrument) is not None
    assert "(0 days)" in (calendar_warning(through, instrument) or "")


@pytest.mark.parametrize("hour", [9, 10])
def test_trade_management_closes_out_before_an_early_close(
    hour: int, instrument: InstrumentProfile
) -> None:
    from footprint.execution.management.malik import Manager

    class P:
        pass

    profiles = P()
    profiles.instrument = instrument  # type: ignore[attr-defined]
    strategy = P()
    strategy.console = P()  # type: ignore[attr-defined]
    strategy.console.close_lead_minutes = 5  # type: ignore[attr-defined]
    profiles.strategy = strategy  # type: ignore[attr-defined]
    manager = Manager.__new__(Manager)
    manager._p = profiles  # type: ignore[assignment]
    at = pt(FRI_AFTER_THANKSGIVING, hour, 56 if hour == 9 else 0)
    assert manager._past_close(at)  # 09:56 and 10:00 are past the 09:55 cutoff
    assert not manager._past_close(pt(FRI_AFTER_THANKSGIVING, 9, 54))
