"""Never-break rules owned by the risk layer and session guard.

Each test is an attack on one rule from the approved never-break spec
(Footprint — Never-Break Tests & Money Model, approved by Andre on 2026-10-04).
The IDs (NB-xx) match that spec. If any of these fails, nothing ships.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.core.config import InstrumentProfile, RiskProfile
from footprint.core.models import Direction
from footprint.decision.risk.gate import AccountState, EntryRequest, Veto, evaluate
from footprint.decision.session import (
    NewsBlackout,
    SessionKind,
    in_news_blackout,
    overnight_must_be_flat,
    session_at,
)


def test_baseline_clean_state_and_good_entry_is_allowed(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """Control case: without it, every 'blocked' test below could pass vacuously."""
    assert evaluate(risk, clean_state, good_entry).allowed


@pytest.mark.parametrize("direction", list(Direction))
def test_nb05_never_add_to_a_loser(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest, direction: Direction
) -> None:
    """NB-05: with a position open (losing or not), no second entry in any direction."""
    state = replace(clean_state, position_open=True)
    decision = evaluate(risk, state, replace(good_entry, direction=direction))
    assert not decision.allowed
    assert Veto.POSITION_OPEN in decision.vetoes


def test_nb06_no_entry_in_news_blackout_via_gate(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-06: a perfect signal inside a news blackout is blocked."""
    decision = evaluate(risk, clean_state, replace(good_entry, in_news_blackout=True))
    assert Veto.NEWS_BLACKOUT in decision.vetoes


def test_nb06_blackout_windows_from_the_spec() -> None:
    """NB-06: 8:30 ET release (blackout from 5:10 AM PT) and Fed day (11:00-11:40 AM PT).

    Window edges come from the calendar, so the test supplies them as the spec states.
    """
    cpi = NewsBlackout(  # 2026-10-06, 05:10-05:45 PT
        "CPI",
        start=datetime(2026, 10, 6, 12, 10, tzinfo=UTC),
        end=datetime(2026, 10, 6, 12, 45, tzinfo=UTC),
    )
    fed = NewsBlackout(  # 11:00-11:40 PT
        "FOMC",
        start=datetime(2026, 10, 6, 18, 0, tzinfo=UTC),
        end=datetime(2026, 10, 6, 18, 40, tzinfo=UTC),
    )
    blackouts = (cpi, fed)
    assert in_news_blackout(datetime(2026, 10, 6, 12, 15, tzinfo=UTC), blackouts) is cpi  # 5:15 PT
    assert in_news_blackout(datetime(2026, 10, 6, 18, 10, tzinfo=UTC), blackouts) is fed  # 11:10
    assert in_news_blackout(cpi.start, blackouts) is cpi  # edges inclusive
    assert in_news_blackout(fed.end, blackouts) is fed
    assert in_news_blackout(datetime(2026, 10, 6, 12, 9, tzinfo=UTC), blackouts) is None


def test_nb07_overnight_flat_by_session_end(instrument: InstrumentProfile) -> None:
    """NB-07: an overnight position must be flat at the overnight session's last minute."""
    # Wednesday 2026-10-07, Pacific daylight time (UTC-7).
    assert not overnight_must_be_flat(datetime(2026, 10, 7, 10, 58, tzinfo=UTC), instrument)
    assert overnight_must_be_flat(datetime(2026, 10, 7, 10, 59, tzinfo=UTC), instrument)  # 03:59
    assert overnight_must_be_flat(datetime(2026, 10, 7, 11, 0, tzinfo=UTC), instrument)  # 04:00
    assert overnight_must_be_flat(datetime(2026, 10, 7, 13, 30, tzinfo=UTC), instrument)  # prime


def test_nb08_two_stopouts_end_the_day(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-08: after the daily stop-out limit, even a perfect signal is blocked."""
    state = replace(clean_state, stopouts_today=risk.daily_max_stopouts)
    assert Veto.DAILY_STOPOUTS in evaluate(risk, state, good_entry).vetoes
    one_short = replace(clean_state, stopouts_today=risk.daily_max_stopouts - 1)
    assert evaluate(risk, one_short, good_entry).allowed


def test_nb09_daily_loss_limit(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-09: at the daily loss limit (or past it), no new entries."""
    at_limit = replace(clean_state, realized_pnl_today=-risk.daily_max_loss)
    past_limit = replace(clean_state, realized_pnl_today=-risk.daily_max_loss - Decimal(1))
    for state in (at_limit, past_limit):
        assert Veto.DAILY_LOSS in evaluate(risk, state, good_entry).vetoes
    just_inside = replace(clean_state, realized_pnl_today=-risk.daily_max_loss + Decimal(1))
    assert evaluate(risk, just_inside, good_entry).allowed


def test_nb10_weekly_loss_limit(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-10: at the weekly loss limit, no new entries."""
    state = replace(clean_state, realized_pnl_week=-risk.weekly_max_loss)
    assert Veto.WEEKLY_LOSS in evaluate(risk, state, good_entry).vetoes


def test_nb11_kill_switch_trips_on_drawdown(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-11: equity down the kill-switch percentage from peak stops all trading."""
    peak = risk.bankroll
    trip = peak - peak * risk.kill_switch_drawdown_pct / Decimal(100)
    state = replace(clean_state, equity=trip, peak_equity=peak)
    assert Veto.KILL_SWITCH in evaluate(risk, state, good_entry).vetoes
    above = replace(clean_state, equity=trip + Decimal(1), peak_equity=peak)
    assert Veto.KILL_SWITCH not in evaluate(risk, above, good_entry).vetoes


def test_nb11_kill_switch_stays_latched_after_recovery(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-11: once latched, recovering equity does not re-enable trading; only Andre can."""
    state = replace(clean_state, kill_switch_latched=True)
    assert Veto.KILL_SWITCH in evaluate(risk, state, good_entry).vetoes


def test_nb13_prime_trade_ceiling(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-13: once the prime trade ceiling is reached, no more prime entries."""
    state = replace(clean_state, trades_today_prime=risk.max_trades_prime)
    assert Veto.TRADE_CEILING in evaluate(risk, state, good_entry).vetoes


def test_nb13_no_overnight_trades_in_v1(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-13 (v1): overnight trade ceiling is 0, so every overnight entry is blocked."""
    assert risk.max_trades_overnight == 0
    entry = replace(good_entry, session=SessionKind.OVERNIGHT, cost=risk.overnight_position_cap)
    assert Veto.TRADE_CEILING in evaluate(risk, clean_state, entry).vetoes


def test_nb17_cost_over_position_cap_is_blocked(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """NB-17: an entry costing more than the cap is never allowed through."""
    over = replace(good_entry, cost=risk.position_cap + Decimal("0.01"))
    assert Veto.OVER_POSITION_CAP in evaluate(risk, clean_state, over).vetoes
    assert evaluate(risk, clean_state, replace(good_entry, cost=risk.position_cap)).allowed


def test_no_entry_when_market_session_closed(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    decision = evaluate(risk, clean_state, replace(good_entry, session=SessionKind.CLOSED))
    assert Veto.SESSION_CLOSED in decision.vetoes


def test_every_veto_is_reported_not_just_the_first(
    risk: RiskProfile, clean_state: AccountState, good_entry: EntryRequest
) -> None:
    """The decision log must show the full picture, so all checks run."""
    state = replace(
        clean_state,
        stopouts_today=risk.daily_max_stopouts,
        realized_pnl_today=-risk.daily_max_loss,
        position_open=True,
    )
    vetoes = set(evaluate(risk, state, replace(good_entry, in_news_blackout=True)).vetoes)
    assert {Veto.DAILY_STOPOUTS, Veto.DAILY_LOSS, Veto.POSITION_OPEN, Veto.NEWS_BLACKOUT} <= vetoes


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 6, 11, 0, tzinfo=UTC), SessionKind.PRIME),  # 04:00 PDT
        (datetime(2026, 10, 6, 21, 0, tzinfo=UTC), SessionKind.PRIME),  # 14:00 PDT
        (datetime(2026, 10, 6, 21, 1, tzinfo=UTC), SessionKind.CLOSED),  # 14:01 PDT
        (datetime(2026, 10, 6, 22, 0, tzinfo=UTC), SessionKind.OVERNIGHT),  # 15:00 PDT
        (datetime(2026, 10, 7, 10, 59, tzinfo=UTC), SessionKind.OVERNIGHT),  # 03:59 PDT
        (datetime(2026, 1, 6, 14, 30, tzinfo=UTC), SessionKind.PRIME),  # 06:30 PST (winter)
        (datetime(2026, 1, 6, 12, 30, tzinfo=UTC), SessionKind.PRIME),  # 04:30 PST
        (datetime(2026, 1, 6, 11, 30, tzinfo=UTC), SessionKind.OVERNIGHT),  # 03:30 PST
    ],
)
def test_session_windows_follow_pacific_time_across_dst(
    instrument: InstrumentProfile, utc: datetime, expected: SessionKind
) -> None:
    assert session_at(utc, instrument) is expected


def test_session_guard_rejects_naive_timestamps(instrument: InstrumentProfile) -> None:
    with pytest.raises(ValueError, match="UTC"):
        session_at(datetime(2026, 10, 6, 13, 30), instrument)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="UTC"):
        overnight_must_be_flat(datetime(2026, 10, 6, 13, 30), instrument)  # noqa: DTZ001


def test_news_blackout_rejects_inverted_window() -> None:
    start = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="end"):
        NewsBlackout("CPI", start=start, end=start - timedelta(minutes=1))


@pytest.mark.parametrize(
    "changes",
    [
        {"stopouts_today": -1},
        {"stopouts_today": True},
        {"equity": 1.0},
        {"peak_equity": Decimal(0)},
        {"equity": Decimal("30000"), "peak_equity": Decimal("25000")},
        {"position_open": 1},
    ],
)
def test_account_state_rejects_bad_values(
    clean_state: AccountState, changes: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(clean_state, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"cost": Decimal(0)},
        {"cost": Decimal("NaN")},
        {"cost": 100},
        {"ts": datetime(2026, 10, 6, 13, 30)},  # noqa: DTZ001 - naive on purpose
        {"session": "PRIME"},
        {"in_news_blackout": 0},
    ],
)
def test_entry_request_rejects_bad_values(
    good_entry: EntryRequest, changes: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(good_entry, **changes)  # type: ignore[arg-type]
