"""Risk layer (Dolores): the final, deterministic veto on every new entry.

The gate is a pure function of the profiles, the news calendar, the account's
state and the proposed entry. It works out the session and any news blackout
from the entry's own timestamp; it never trusts a caller's label for either.
It never sizes, prices or places anything; it only answers "may this entry
proceed?" and, when not, every reason why.

Caller responsibilities (not yet built; see tests/never_break/README.md):
- keep `peak_equity` as the all-time peak since Andre last reset the account,
- persist the kill-switch latch when a decision says `latch_kill_switch`,
- reset the daily and weekly counters at the session boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from footprint.core.config import InstrumentProfile, RiskProfile
from footprint.core.models import Direction, require_utc
from footprint.decision.session import (
    NewsBlackout,
    SessionKind,
    in_news_blackout,
    session_at,
)


class Veto(Enum):
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_STOPOUTS = "DAILY_STOPOUTS"
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    POSITION_OPEN = "POSITION_OPEN"
    TRADE_CEILING = "TRADE_CEILING"
    SESSION_CLOSED = "SESSION_CLOSED"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"
    OVER_POSITION_CAP = "OVER_POSITION_CAP"


def _require_decimal(value: Decimal, field: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite, got {value}")
    if positive and value <= 0:
        raise ValueError(f"{field} must be > 0, got {value}")


def _require_count(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field} must be >= 0, got {value}")


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the risk layer needs to know about the account at decision time.

    Realized P&L figures are signed (a loss is negative). `kill_switch_latched`
    stays True once set until Andre clears it by hand; the gate never clears it.
    """

    equity: Decimal
    peak_equity: Decimal
    realized_pnl_today: Decimal
    realized_pnl_week: Decimal
    stopouts_today: int
    trades_today_prime: int
    trades_today_overnight: int
    position_open: bool
    kill_switch_latched: bool

    def __post_init__(self) -> None:
        _require_decimal(self.equity, "AccountState.equity")
        _require_decimal(self.peak_equity, "AccountState.peak_equity", positive=True)
        _require_decimal(self.realized_pnl_today, "AccountState.realized_pnl_today")
        _require_decimal(self.realized_pnl_week, "AccountState.realized_pnl_week")
        if self.equity > self.peak_equity:
            raise ValueError("AccountState.peak_equity must be >= equity")
        _require_count(self.stopouts_today, "AccountState.stopouts_today")
        _require_count(self.trades_today_prime, "AccountState.trades_today_prime")
        _require_count(self.trades_today_overnight, "AccountState.trades_today_overnight")
        for name in ("position_open", "kill_switch_latched"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"AccountState.{name} must be a bool")


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """A proposed new entry. Session and blackout are derived from `ts`, never supplied."""

    ts: datetime
    direction: Direction
    cost: Decimal

    def __post_init__(self) -> None:
        require_utc(self.ts, "EntryRequest.ts")
        if not isinstance(self.direction, Direction):
            raise TypeError("EntryRequest.direction must be a Direction")
        _require_decimal(self.cost, "EntryRequest.cost", positive=True)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    vetoes: tuple[Veto, ...]
    session: SessionKind
    blackout: NewsBlackout | None
    latch_kill_switch: bool

    @property
    def allowed(self) -> bool:
        return not self.vetoes


def drawdown_breached(profile: RiskProfile, state: AccountState) -> bool:
    """True when equity is down at least the kill-switch percentage from its peak.

    Multiplication only (no division), so the comparison is exact in Decimal.
    """
    trigger = state.peak_equity * profile.kill_switch_drawdown_pct.scaleb(-2)
    return state.peak_equity - state.equity >= trigger


def precheck(
    profile: RiskProfile,
    instrument: InstrumentProfile,
    blackouts: Iterable[NewsBlackout],
    state: AccountState,
    ts: datetime,
) -> RiskDecision:
    """Every veto that does not depend on a sized entry, at moment `ts`.

    Run on every decision, so a latched kill switch or a tripped breaker is always
    on the record even when no setup appears.
    """
    require_utc(ts, "ts")
    session = session_at(ts, instrument)
    blackout = in_news_blackout(ts, blackouts)
    breached = drawdown_breached(profile, state)

    vetoes: list[Veto] = []
    if state.kill_switch_latched or breached:
        vetoes.append(Veto.KILL_SWITCH)
    if state.stopouts_today >= profile.daily_max_stopouts:
        vetoes.append(Veto.DAILY_STOPOUTS)
    if state.realized_pnl_today <= -profile.daily_max_loss:
        vetoes.append(Veto.DAILY_LOSS)
    if state.realized_pnl_week <= -profile.weekly_max_loss:
        vetoes.append(Veto.WEEKLY_LOSS)
    if state.position_open:
        vetoes.append(Veto.POSITION_OPEN)
    if session is SessionKind.CLOSED:
        vetoes.append(Veto.SESSION_CLOSED)
    elif session is SessionKind.PRIME:
        if state.trades_today_prime >= profile.max_trades_prime:
            vetoes.append(Veto.TRADE_CEILING)
    elif state.trades_today_overnight >= profile.max_trades_overnight:
        vetoes.append(Veto.TRADE_CEILING)
    if blackout is not None:
        vetoes.append(Veto.NEWS_BLACKOUT)
    return RiskDecision(
        vetoes=tuple(vetoes),
        session=session,
        blackout=blackout,
        latch_kill_switch=breached and not state.kill_switch_latched,
    )


def evaluate(
    profile: RiskProfile,
    instrument: InstrumentProfile,
    blackouts: Iterable[NewsBlackout],
    state: AccountState,
    entry: EntryRequest,
) -> RiskDecision:
    """Every reason the entry must not proceed; no vetoes means it may.

    All checks always run, so the decision log shows the complete picture.
    """
    base = precheck(profile, instrument, blackouts, state, entry.ts)
    vetoes = list(base.vetoes)
    if base.session is SessionKind.PRIME and entry.cost > profile.position_cap:
        vetoes.append(Veto.OVER_POSITION_CAP)
    if base.session is SessionKind.OVERNIGHT and entry.cost > profile.overnight_position_cap:
        vetoes.append(Veto.OVER_POSITION_CAP)
    return RiskDecision(
        vetoes=tuple(vetoes),
        session=base.session,
        blackout=base.blackout,
        latch_kill_switch=base.latch_kill_switch,
    )
