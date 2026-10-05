"""Never-break rules owned by the risk layer and session guard.

Each test is an attack on one rule from the approved never-break spec
(Footprint — Never-Break Tests & Money Model, approved by Andre on 2026-10-04).
The IDs (NB-xx) match that spec. If any of these fails, nothing ships.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from footprint.core.config import InstrumentProfile, RiskProfile
from footprint.core.models import Direction
from footprint.decision.risk.gate import (
    AccountState,
    EntryRequest,
    RiskDecision,
    Veto,
    drawdown_breached,
    evaluate,
)
from footprint.decision.session import NewsBlackout, SessionKind, in_news_blackout
from tests.never_break.conftest import OVERNIGHT_TS, PRIME_TS

NO_NEWS: tuple[NewsBlackout, ...] = ()

# Spec examples: 8:30 ET release -> blackout from 5:10 AM PT; Fed day 11:00-11:40 AM PT.
CPI = NewsBlackout(
    "CPI",
    start=datetime(2026, 10, 6, 12, 10, tzinfo=UTC),  # 05:10 PT
    end=datetime(2026, 10, 6, 12, 45, tzinfo=UTC),
)
FOMC = NewsBlackout(
    "FOMC",
    start=datetime(2026, 10, 6, 18, 0, tzinfo=UTC),  # 11:00 PT
    end=datetime(2026, 10, 6, 18, 40, tzinfo=UTC),  # 11:40 PT
)


def check(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    state: AccountState,
    entry: EntryRequest,
    blackouts: Sequence[NewsBlackout] = NO_NEWS,
) -> RiskDecision:
    return evaluate(risk, instrument, blackouts, state, entry)


def test_baseline_clean_state_and_good_entry_is_allowed(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """Control case: without it, every 'blocked' test below could pass vacuously."""
    decision = check(risk, instrument, clean_state, good_entry)
    assert decision.allowed
    assert decision.session is SessionKind.PRIME


@pytest.mark.parametrize("direction", list(Direction))
def test_nb05_never_add_to_a_loser(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
    direction: Direction,
) -> None:
    """NB-05: with a position open (losing or not), no second entry in any direction."""
    state = replace(clean_state, position_open=True)
    decision = check(risk, instrument, state, replace(good_entry, direction=direction))
    assert Veto.POSITION_OPEN in decision.vetoes


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (datetime(2026, 10, 6, 12, 15, tzinfo=UTC), CPI),  # 05:15 PT
        (datetime(2026, 10, 6, 18, 10, tzinfo=UTC), FOMC),  # 11:10 PT
        (CPI.start, CPI),  # edges are inclusive
        (FOMC.end, FOMC),
    ],
)
def test_nb06_no_entry_in_news_blackout(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
    ts: datetime,
    expected: NewsBlackout,
) -> None:
    """NB-06: the gate finds the blackout from the entry's own time; the caller can't hide it."""
    decision = check(risk, instrument, clean_state, replace(good_entry, ts=ts), (CPI, FOMC))
    assert Veto.NEWS_BLACKOUT in decision.vetoes
    assert decision.blackout is expected


def test_nb06_just_outside_blackout_is_allowed(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    ts = datetime(2026, 10, 6, 12, 9, tzinfo=UTC)  # 05:09 PT
    assert in_news_blackout(ts, (CPI, FOMC)) is None
    assert check(risk, instrument, clean_state, replace(good_entry, ts=ts), (CPI, FOMC)).allowed


def test_nb08_two_stopouts_end_the_day(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-08: after the daily stop-out limit, even a perfect signal is blocked."""
    at_limit = replace(clean_state, stopouts_today=risk.daily_max_stopouts)
    assert Veto.DAILY_STOPOUTS in check(risk, instrument, at_limit, good_entry).vetoes
    one_short = replace(clean_state, stopouts_today=risk.daily_max_stopouts - 1)
    assert check(risk, instrument, one_short, good_entry).allowed


def test_nb09_daily_loss_limit(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-09: at the daily loss limit (or past it), no new entries."""
    for pnl in (-risk.daily_max_loss, -risk.daily_max_loss - Decimal(1)):
        state = replace(clean_state, realized_pnl_today=pnl)
        assert Veto.DAILY_LOSS in check(risk, instrument, state, good_entry).vetoes
    inside = replace(clean_state, realized_pnl_today=-risk.daily_max_loss + Decimal(1))
    assert check(risk, instrument, inside, good_entry).allowed


def test_nb10_weekly_loss_limit(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-10: at the weekly loss limit (or past it), no new entries."""
    for pnl in (-risk.weekly_max_loss, -risk.weekly_max_loss - Decimal(1)):
        state = replace(clean_state, realized_pnl_week=pnl)
        assert Veto.WEEKLY_LOSS in check(risk, instrument, state, good_entry).vetoes
    inside = replace(clean_state, realized_pnl_week=-risk.weekly_max_loss + Decimal(1))
    assert check(risk, instrument, inside, good_entry).allowed


def test_nb11_kill_switch_trips_on_drawdown_and_asks_to_latch(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-11: equity down the kill-switch percentage from peak stops trading and must latch."""
    peak = risk.bankroll
    trip = peak - peak * risk.kill_switch_drawdown_pct / Decimal(100)
    tripped = replace(clean_state, equity=trip, peak_equity=peak)
    decision = check(risk, instrument, tripped, good_entry)
    assert Veto.KILL_SWITCH in decision.vetoes
    assert decision.latch_kill_switch
    above = replace(clean_state, equity=trip + Decimal("0.01"), peak_equity=peak)
    assert not drawdown_breached(risk, above)
    assert check(risk, instrument, above, good_entry).allowed


def test_nb11_latched_kill_switch_blocks_even_after_recovery(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-11: once latched, full equity does not re-enable trading; only Andre can."""
    state = replace(clean_state, kill_switch_latched=True)
    decision = check(risk, instrument, state, good_entry)
    assert Veto.KILL_SWITCH in decision.vetoes
    assert not decision.latch_kill_switch  # already latched; nothing new to persist


@pytest.mark.parametrize("equity", [Decimal(0), Decimal(-500)])
def test_nb11_zero_or_negative_equity_trips(
    risk: RiskProfile, clean_state: AccountState, equity: Decimal
) -> None:
    assert drawdown_breached(risk, replace(clean_state, equity=equity))


def test_nb13_prime_trade_ceiling(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-13: at the prime trade ceiling, no more prime entries; one under is fine."""
    at_limit = replace(clean_state, trades_today_prime=risk.max_trades_prime)
    assert Veto.TRADE_CEILING in check(risk, instrument, at_limit, good_entry).vetoes
    one_under = replace(clean_state, trades_today_prime=risk.max_trades_prime - 1)
    assert check(risk, instrument, one_under, good_entry).allowed


def test_nb13_no_overnight_trades_in_v1(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-13 (v1): an entry at an overnight time is blocked, whatever the caller intends."""
    assert risk.max_trades_overnight == 0
    entry = replace(good_entry, ts=OVERNIGHT_TS, cost=risk.overnight_position_cap)
    decision = check(risk, instrument, clean_state, entry)
    assert decision.session is SessionKind.OVERNIGHT
    assert Veto.TRADE_CEILING in decision.vetoes


def test_nb17_cost_over_position_cap_is_blocked(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-17: an entry costing more than the prime cap is never allowed through."""
    over = replace(good_entry, cost=risk.position_cap + Decimal("0.01"))
    assert Veto.OVER_POSITION_CAP in check(risk, instrument, clean_state, over).vetoes
    assert check(risk, instrument, clean_state, replace(good_entry, cost=risk.position_cap)).allowed


def test_nb17_overnight_cap_is_enforced_when_overnight_trading_is_enabled(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """NB-17: the overnight cap holds even if a later version allows overnight trades."""
    later = replace(risk, max_trades_overnight=1)
    at_cap = replace(good_entry, ts=OVERNIGHT_TS, cost=later.overnight_position_cap)
    assert check(later, instrument, clean_state, at_cap).allowed
    over = replace(at_cap, cost=later.overnight_position_cap + Decimal("0.01"))
    assert Veto.OVER_POSITION_CAP in check(later, instrument, clean_state, over).vetoes


@pytest.mark.parametrize(
    "ts",
    [
        datetime(2026, 10, 10, 17, 0, tzinfo=UTC),  # Sat 10:00 PT
        datetime(2026, 10, 11, 17, 0, tzinfo=UTC),  # Sun 10:00 PT
        datetime(2026, 10, 9, 22, 30, tzinfo=UTC),  # Fri 15:30 PT (into Saturday)
        datetime(2026, 10, 6, 21, 30, tzinfo=UTC),  # Tue 14:30 PT (daily break)
    ],
)
def test_no_entry_when_market_is_closed(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
    ts: datetime,
) -> None:
    decision = check(risk, instrument, clean_state, replace(good_entry, ts=ts))
    assert decision.session is SessionKind.CLOSED
    assert Veto.SESSION_CLOSED in decision.vetoes


def test_holiday_is_closed_including_the_evening_before(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    xmas = replace(instrument, holidays=frozenset({date(2026, 12, 25)}))
    day = replace(good_entry, ts=datetime(2026, 12, 25, 15, 0, tzinfo=UTC))  # Fri 07:00 PST
    eve = replace(good_entry, ts=datetime(2026, 12, 25, 0, 0, tzinfo=UTC))  # Thu 16:00 PST
    for entry in (day, eve):
        assert Veto.SESSION_CLOSED in check(risk, xmas, clean_state, entry).vetoes


def test_every_veto_is_reported_not_just_the_first(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    clean_state: AccountState,
    good_entry: EntryRequest,
) -> None:
    """The decision log must show the full picture, so all checks run."""
    state = replace(
        clean_state,
        stopouts_today=risk.daily_max_stopouts,
        realized_pnl_today=-risk.daily_max_loss,
        position_open=True,
    )
    entry = replace(good_entry, ts=datetime(2026, 10, 6, 12, 15, tzinfo=UTC))
    vetoes = set(check(risk, instrument, state, entry, (CPI,)).vetoes)
    assert {Veto.DAILY_STOPOUTS, Veto.DAILY_LOSS, Veto.POSITION_OPEN, Veto.NEWS_BLACKOUT} <= vetoes


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
        {"ts": PRIME_TS.replace(tzinfo=None)},
        {"direction": "LONG"},
    ],
)
def test_entry_request_rejects_bad_values(
    good_entry: EntryRequest, changes: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(good_entry, **changes)  # type: ignore[arg-type]
