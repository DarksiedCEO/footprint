"""Theta (decay veto): is the time decay over the planned hold worth paying?

Andre's decisions B76-1 and B76-2. After Felix has chosen the contract and Ezra has
sized it, and before Dolores' full risk check:

- The option's expiry moment is the instrument's rule (core/expiry.py). An option of a
  quarterly (American) trading class is refused: Black-76 does not value it (B76-2).
- Time is calendar time to that moment (B76-1), in years of 365 days. An option that
  has expired, or that expires within `planned_hold_minutes`, is refused.
- The implied volatility is the Black-76 volatility at which the option is worth its
  quote's mid, with the chain's underlying price as the forward and the configured
  rate, solved inside [min_implied_vol, max_implied_vol]. No solution in that range
  (an extreme volatility, or a quote below its intrinsic value) is refused: these are
  the hard dangers Andre approved for the Greeks agents.
- The modelled decay is the option's Black-76 value now minus its value at the end of
  the hold, at the same forward and volatility (the model's price difference over the
  hold, not theta times the hold: theta is an instantaneous rate). In dollars it is
  that, times the point value, times the sized contracts.
- Decay above max_decay_pct_of_target of risk.profit_target is refused. At or below it
  the entry may go on.

Every number is a model estimate made at one moment: it sizes no order and is never
booked (never book a guessed price). The check fails closed: anything it cannot work
out means no entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from footprint.core import black76
from footprint.core.black76 import Right76
from footprint.core.config import _PERCENT_MAX, InstrumentProfile, RiskProfile, ThetaSettings
from footprint.core.expiry import expiry_moment, is_american
from footprint.core.implied_vol import ImpliedVolOutOfRange, implied_vol
from footprint.core.models import require_utc
from footprint.core.options import Right
from footprint.decision.sizing import EntryPlan

_YEAR = timedelta(days=365)  # B76-1: calendar years


@dataclass(frozen=True, slots=True)
class ThetaCheck:
    """Theta's verdict on one sized entry, with the numbers behind it (None where the
    check stopped before working them out)."""

    allowed: bool
    reason: str
    contract_id: str
    contracts: int
    expires_at: datetime | None
    years: float | None
    implied_vol: float | None
    decay_points: float | None  # per option, in option points
    decay_dollars: Decimal | None  # for the sized contracts
    budget_dollars: Decimal


class ThetaAgent:
    def __init__(
        self, settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile
    ) -> None:
        self._s = settings
        self._instrument = instrument
        self._budget = risk.profit_target * settings.max_decay_pct_of_target / _PERCENT_MAX

    @property
    def agent_id(self) -> str:
        return "theta"

    def assess(self, plan: EntryPlan, underlying_price: Decimal, as_of: datetime) -> ThetaCheck:
        require_utc(as_of, "as_of")
        quote = plan.choice.quote
        s = self._s

        def verdict(
            allowed: bool,
            reason: str,
            *,
            expires_at: datetime | None = None,
            years: float | None = None,
            vol: float | None = None,
            decay_points: float | None = None,
            decay_dollars: Decimal | None = None,
        ) -> ThetaCheck:
            return ThetaCheck(
                allowed=allowed,
                reason=reason,
                contract_id=quote.contract_id,
                contracts=plan.contracts,
                expires_at=expires_at,
                years=years,
                implied_vol=vol,
                decay_points=decay_points,
                decay_dollars=decay_dollars,
                budget_dollars=self._budget,
            )

        if is_american(quote, self._instrument):
            return verdict(
                False,
                f"{quote.contract_id}: trading class {quote.trading_class} is a quarterly "
                "(American) option; Black-76 does not value it (B76-2)",
            )
        expires_at = expiry_moment(quote.expiry, self._instrument)
        left = expires_at - as_of
        hold = timedelta(minutes=s.planned_hold_minutes)
        if left <= hold:
            return verdict(
                False,
                f"{quote.contract_id} expires at {expires_at.isoformat()}, within the "
                f"planned {s.planned_hold_minutes}-minute hold",
                expires_at=expires_at,
            )
        years = left / _YEAR
        right = Right76.CALL if quote.right is Right.CALL else Right76.PUT
        forward = float(underlying_price)
        strike = float(quote.strike)
        rate = float(s.rate)
        try:
            vol = implied_vol(
                right,
                forward=forward,
                strike=strike,
                years=years,
                rate=rate,
                price=float(quote.mid),
                low=float(s.min_implied_vol),
                high=float(s.max_implied_vol),
            )
        except ImpliedVolOutOfRange as exc:
            bound = s.min_implied_vol if exc.side == "below" else s.max_implied_vol
            return verdict(
                False,
                f"{quote.contract_id}: implied volatility of mid {quote.mid} is {exc.side} "
                f"the {bound} bound (hard danger)",
                expires_at=expires_at,
                years=years,
            )
        except ValueError as exc:  # inputs the model cannot take: fail closed
            return verdict(
                False,
                f"{quote.contract_id}: cannot value the option: {exc}",
                expires_at=expires_at,
                years=years,
            )

        def value(time_left: float) -> float:
            return black76.price(
                right, forward=forward, strike=strike, years=time_left, vol=vol, rate=rate
            )

        decay_points = value(years) - value(years - hold / _YEAR)
        decay_dollars = Decimal(str(decay_points)) * self._instrument.point_value * plan.contracts
        over = decay_dollars > self._budget
        if over:
            reason = (
                f"{quote.contract_id}: modelled decay over the {s.planned_hold_minutes}-minute "
                f"hold is {decay_dollars:.2f} for {plan.contracts} contract(s), over the "
                f"{self._budget} budget"
            )
        else:
            reason = (
                f"{quote.contract_id}: modelled decay {decay_dollars:.2f} within the "
                f"{self._budget} budget"
            )
        return verdict(
            not over,
            reason,
            expires_at=expires_at,
            years=years,
            vol=vol,
            decay_points=decay_points,
            decay_dollars=decay_dollars,
        )
