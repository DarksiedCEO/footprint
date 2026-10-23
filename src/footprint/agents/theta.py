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

The same valuation (`value_decay`) serves Theta's decay exit (decision DX-1,
`DecayExit`): trade management asks it, on a fresh quote, whether an open trade in
profit but short of the target is losing more to decay over the next window than the
configured share of its open profit; see `DecayExit`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from footprint.core import black76
from footprint.core.black76 import Right76
from footprint.core.config import (
    _PERCENT_MAX,
    InstrumentProfile,
    QuoteSettings,
    RiskProfile,
    ThetaSettings,
)
from footprint.core.expiry import expiry_moment, is_american, is_european_class
from footprint.core.implied_vol import ImpliedVolOutOfRange, implied_vol
from footprint.core.models import require_utc
from footprint.core.options import OptionChain, OptionQuote, Right
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
        valued = value_decay(
            quote,
            underlying_price,
            as_of,
            timedelta(minutes=s.planned_hold_minutes),
            f"planned {s.planned_hold_minutes}-minute hold",
            s,
            self._instrument,
        )

        def verdict(allowed: bool, reason: str, decay_dollars: Decimal | None = None) -> ThetaCheck:
            return ThetaCheck(
                allowed=allowed,
                reason=reason,
                contract_id=quote.contract_id,
                contracts=plan.contracts,
                expires_at=valued.expires_at,
                years=valued.years,
                implied_vol=valued.implied_vol,
                decay_points=valued.decay_points,
                decay_dollars=decay_dollars,
                budget_dollars=self._budget,
            )

        if valued.decay_points is None:
            return verdict(False, valued.reason)
        points = valued.decay_points
        decay_dollars = Decimal(str(points)) * self._instrument.point_value * plan.contracts
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
        return verdict(not over, reason, decay_dollars)


@dataclass(frozen=True, slots=True)
class DecayValue:
    """One option's modelled decay over a window (B76-1), or why it cannot be valued:
    `decay_points` is None exactly when it cannot, and `reason` then says why. The
    other numbers are those worked out before the valuation stopped (None if not)."""

    reason: str
    expires_at: datetime | None
    years: float | None
    implied_vol: float | None
    decay_points: float | None  # per option, in option points


def value_decay(
    quote: OptionQuote,
    underlying_price: Decimal,
    as_of: datetime,
    window: timedelta,
    window_name: str,
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> DecayValue:
    """The Black-76 value of `quote` now minus its value `window` later (decisions
    B76-1, B76-2): calendar time to the option's expiry moment, at the implied
    volatility of the quote's mid, with `underlying_price` as the forward. Refused (no
    decay): a quarterly (American) or unknown class, an option that expires within
    the window (`window_name` names it in the reason), no implied volatility inside
    [min_implied_vol, max_implied_vol], or inputs the model cannot take."""

    def refused(
        reason: str, *, expires_at: datetime | None = None, years: float | None = None
    ) -> DecayValue:
        return DecayValue(reason, expires_at, years, None, None)

    if is_american(quote, instrument):
        return refused(
            f"{quote.contract_id}: trading class {quote.trading_class} is a quarterly "
            "(American) option; Black-76 does not value it (B76-2)",
        )
    if not is_european_class(quote.trading_class, instrument):
        return refused(
            f"{quote.contract_id}: trading class {quote.trading_class} is not a known "
            "European class (option_expiry.european_trading_class_patterns); not valued",
        )
    expires_at = expiry_moment(quote.expiry, instrument)
    left = expires_at - as_of
    if left <= window:
        return refused(
            f"{quote.contract_id} expires at {expires_at.isoformat()}, within the {window_name}",
            expires_at=expires_at,
        )
    years = left / _YEAR
    right = Right76.CALL if quote.right is Right.CALL else Right76.PUT
    forward = float(underlying_price)
    strike = float(quote.strike)
    rate = float(settings.rate)
    try:
        vol = implied_vol(
            right,
            forward=forward,
            strike=strike,
            years=years,
            rate=rate,
            price=float(quote.mid),
            low=float(settings.min_implied_vol),
            high=float(settings.max_implied_vol),
        )
    except ImpliedVolOutOfRange as exc:
        bound = settings.min_implied_vol if exc.side == "below" else settings.max_implied_vol
        return refused(
            f"{quote.contract_id}: implied volatility of mid {quote.mid} is {exc.side} "
            f"the {bound} bound (hard danger)",
            expires_at=expires_at,
            years=years,
        )
    except ValueError as exc:  # inputs the model cannot take: fail closed
        return refused(
            f"{quote.contract_id}: cannot value the option: {exc}",
            expires_at=expires_at,
            years=years,
        )

    def value(time_left: float) -> float:
        return black76.price(
            right, forward=forward, strike=strike, years=time_left, vol=vol, rate=rate
        )

    decay_points = value(years) - value(years - window / _YEAR)
    return DecayValue("", expires_at, years, vol, decay_points)


@dataclass(frozen=True, slots=True)
class DecayExitCheck:
    """The decay exit's answer for one open position on one quote, with the numbers
    behind it (None where the check stopped before working them out). `fire`: close
    the trade now (trade management's exit path does it); `reason` is the journal line."""

    fire: bool
    # True: a trade the rule judges, whose option could not be valued (`reason` says
    # why); the decay exit is silent for it. False: fired, or stayed by the rule.
    unvalued: bool
    reason: str
    open_profit: Decimal
    limit_dollars: Decimal | None  # max_pct_of_open_profit of the open profit
    decay_dollars: Decimal | None  # over the window, for the held contracts
    decay_points: float | None  # per option, in option points
    implied_vol: float | None
    years: float | None
    underlying_price: Decimal | None


class DecayExit:
    """Theta's decay exit (decision DX-1), asked by trade management on a fresh quote.

    Off (`decay_exit.enabled` false): never says out, and values nothing.

    Only a trade in profit and short of the target is judged: its open profit (trade
    management's figure: on the bid, net of round-trip fees, the basis of the trail and
    of every risk check) above 0 and below risk.profit_target, with Booker's trail not
    locked. A losing trade is left to its stop; a locked trail manages the rest.

    The held option is found in the option chain at `now` (its strike, expiry, right,
    trading class and quote, and the chain's underlying price) and valued exactly as
    Theta values an entry (`value_decay`), over the next `decay_exit.window_minutes`.
    That decay, times the point value and the held contracts, above
    `decay_exit.max_pct_of_open_profit` percent of the open profit says "out"; at or
    below it, stay.

    Fail closed: no chain, a chain older than quotes.max_age_seconds (or from the
    future), the option not in it, or an option `value_decay` refuses (an American or
    unknown class, expiring within the window, no implied volatility in range) says
    "stay" with `unvalued` set (trade management journals it once per position):
    never sell on a guess; the other exits keep running. Every number is a
    model estimate: it decides an exit, never a price (the exit sells at the bid)."""

    def __init__(
        self,
        settings: ThetaSettings,
        instrument: InstrumentProfile,
        risk: RiskProfile,
        quotes: QuoteSettings,
        chain: Callable[[datetime], OptionChain | None],
    ) -> None:
        self._s = settings
        self._instrument = instrument
        self._target = risk.profit_target
        self._max_age = timedelta(seconds=quotes.max_age_seconds)
        self._chain = chain

    @property
    def agent_id(self) -> str:
        return "theta"

    def check(
        self,
        contract_id: str,
        contracts: int,
        open_profit: Decimal,
        trailing: bool,
        now: datetime,
    ) -> DecayExitCheck:
        require_utc(now, "now")
        rule = self._s.decay_exit

        def stay(
            reason: str, years: float | None = None, *, unvalued: bool = False
        ) -> DecayExitCheck:
            return DecayExitCheck(
                fire=False,
                unvalued=unvalued,
                reason=reason,
                open_profit=open_profit,
                limit_dollars=None,
                decay_dollars=None,
                decay_points=None,
                implied_vol=None,
                years=years,
                underlying_price=None,
            )

        if not rule.enabled:
            return stay(f"{contract_id}: the decay exit is off (theta.decay_exit.enabled)")
        if open_profit <= 0:
            return stay(f"{contract_id}: not in profit ({open_profit:.2f}); left to its stop")
        if open_profit >= self._target:
            return stay(
                f"{contract_id}: open profit {open_profit:.2f} has reached the "
                f"{self._target} target; left to the trail"
            )
        if trailing:
            return stay(f"{contract_id}: the trail has locked; left to the trail")
        chain = self._chain(now)
        if chain is None:
            return stay(
                f"{contract_id}: no option chain to value it from; no decay exit", unvalued=True
            )
        age = now - chain.ts
        if age < timedelta(0):
            return stay(
                f"{contract_id}: the option chain is from the future "
                f"({chain.ts.isoformat()}); no decay exit",
                unvalued=True,
            )
        if age > self._max_age:
            return stay(
                f"{contract_id}: the option chain is stale ({chain.ts.isoformat()}); no decay exit",
                unvalued=True,
            )
        quote = next((q for q in chain.quotes if q.contract_id == contract_id), None)
        if quote is None:
            return stay(f"{contract_id}: not in the option chain; no decay exit", unvalued=True)
        valued = value_decay(
            quote,
            chain.underlying_price,
            now,
            timedelta(minutes=rule.window_minutes),
            f"{rule.window_minutes}-minute decay-exit window",
            self._s,
            self._instrument,
        )
        if valued.decay_points is None:
            return stay(f"{valued.reason}; no decay exit", valued.years, unvalued=True)
        decay = Decimal(str(valued.decay_points)) * self._instrument.point_value * contracts
        limit = open_profit * rule.max_pct_of_open_profit / _PERCENT_MAX
        fire = decay > limit
        numbers = (
            f"modelled decay over the next {rule.window_minutes} minutes {decay:.2f} for "
            f"{contracts} contract(s), {'above' if fire else 'within'} "
            f"{rule.max_pct_of_open_profit}% of the open profit {open_profit:.2f} (limit "
            f"{limit:.2f}); implied vol {valued.implied_vol:.4f} from mid {quote.mid}, "
            f"underlying {chain.underlying_price}"
        )
        return DecayExitCheck(
            fire=fire,
            unvalued=False,
            reason=f"{contract_id}: decay exit: {numbers}" if fire else f"{contract_id}: {numbers}",
            open_profit=open_profit,
            limit_dollars=limit,
            decay_dollars=decay,
            decay_points=valued.decay_points,
            implied_vol=valued.implied_vol,
            years=valued.years,
            underlying_price=chain.underlying_price,
        )
