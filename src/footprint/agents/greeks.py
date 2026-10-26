"""Vega, Delta and Gamma: the Greeks vetoes (Andre's decision GV-1). Veto only.

After Theta, and before Dolores' full risk check, each agent looks at the contract Felix
chose and Ezra sized, and blocks the entry only on its own hard danger; otherwise it is
silent (it never scores, never sizes, never picks). Each values the option the same way
Theta does:

- the option must be of a known European trading class (B76-2; a quarterly, American
  class or a class never seen is refused, never valued);
- time is calendar time to its expiry moment (core/expiry.py), in years of 365 days
  (B76-1); an option at or past that moment is refused;
- the implied volatility is the Black-76 volatility at which the option is worth its
  quote's mid, with the chain's underlying price as the forward and Theta's configured
  rate, solved (core/implied_vol.py) inside the vega band;
- delta, gamma and vega are Black-76's at that volatility, per option.

The hard dangers:

- Vega: no volatility inside [vega.min_implied_vol, vega.max_implied_vol] prices the
  mid (an extreme volatility, or a quote under its intrinsic value).
- Delta: the absolute delta (a put's delta is negative) is outside
  [delta.min_abs_delta, delta.max_abs_delta]: a far out-of-the-money option that needs a
  big move to pay, or a deep in-the-money one that is mostly intrinsic value.
- Gamma: less than gamma.min_hours_to_expiry is left to the expiry moment, the danger
  zone near expiry where gamma, decay and the gap between a model and the market blow
  up. The rule is on time, not on a gamma number: Black-76 gamma per point falls as the
  index rises, so a fixed gamma cap would drift with the price level, while time to
  expiry is the cause of the danger at any level; and for a bought option a high gamma
  is not itself a loss. Gamma's own rule is checked before the volatility is solved, so
  an option inside the danger zone is always named for it.

Every number is a model estimate made at one moment: it sizes no order and is never
booked (never book a guessed price). Each check fails closed: anything an agent cannot
value means no entry. Every check carries the numbers it worked out, for the journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from footprint.core import black76
from footprint.core.black76 import Black76, Right76
from footprint.core.config import DeltaSettings, GammaSettings, InstrumentProfile, VegaSettings
from footprint.core.expiry import expiry_moment, is_american, is_european_class
from footprint.core.implied_vol import ImpliedVolOutOfRange, implied_vol
from footprint.core.models import require_utc
from footprint.core.options import OptionQuote, Right
from footprint.decision.sizing import EntryPlan

_YEAR = timedelta(days=365)  # B76-1: calendar years
_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class GreekCheck:
    """One Greeks agent's verdict on one sized entry, with the numbers behind it (None
    where the check stopped before working them out)."""

    agent_id: str
    allowed: bool
    reason: str
    contract_id: str
    contracts: int
    expires_at: datetime | None
    years: float | None
    implied_vol: float | None
    delta: float | None  # Black-76, per option; negative for a put
    gamma: float | None  # per futures point, per option
    vega: float | None  # per whole unit of volatility, per option


@dataclass(frozen=True, slots=True)
class _Refused:
    reason: str
    expires_at: datetime | None = None
    years: float | None = None
    out_of_band: ImpliedVolOutOfRange | None = None


@dataclass(frozen=True, slots=True)
class _Valued:
    expires_at: datetime
    years: float
    model: Black76


def _expiry(
    quote: OptionQuote, as_of: datetime, instrument: InstrumentProfile
) -> datetime | _Refused:
    if is_american(quote, instrument):
        return _Refused(
            f"{quote.contract_id}: trading class {quote.trading_class} is a quarterly "
            "(American) option; Black-76 does not value it (B76-2)"
        )
    if not is_european_class(quote.trading_class, instrument):
        return _Refused(
            f"{quote.contract_id}: trading class {quote.trading_class} is not a known "
            "European class (option_expiry.european_trading_class_patterns); not valued"
        )
    expires_at = expiry_moment(quote.expiry, instrument)
    if expires_at <= as_of:
        return _Refused(
            f"{quote.contract_id} expired at {expires_at.isoformat()}", expires_at=expires_at
        )
    return expires_at


def _value(
    quote: OptionQuote,
    underlying_price: Decimal,
    as_of: datetime,
    expires_at: datetime,
    band: VegaSettings,
    rate: Decimal,
) -> _Valued | _Refused:
    years = (expires_at - as_of) / _YEAR
    right = Right76.CALL if quote.right is Right.CALL else Right76.PUT
    try:
        forward = float(underlying_price)
        strike = float(quote.strike)
        vol = implied_vol(
            right,
            forward=forward,
            strike=strike,
            years=years,
            rate=float(rate),
            price=float(quote.mid),
            low=float(band.min_implied_vol),
            high=float(band.max_implied_vol),
        )
        model = black76.greeks(
            right, forward=forward, strike=strike, years=years, vol=vol, rate=float(rate)
        )
    except ImpliedVolOutOfRange as exc:
        bound = band.min_implied_vol if exc.side == "below" else band.max_implied_vol
        return _Refused(
            f"{quote.contract_id}: cannot value the option: implied volatility of mid "
            f"{quote.mid} is {exc.side} the vega band's {bound}",
            expires_at=expires_at,
            years=years,
            out_of_band=exc,
        )
    except ValueError as exc:  # inputs the model cannot take: fail closed
        return _Refused(
            f"{quote.contract_id}: cannot value the option: {exc}",
            expires_at=expires_at,
            years=years,
        )
    return _Valued(expires_at, years, model)


class _GreeksAgent:
    _id: str

    def __init__(self, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile) -> None:
        self._band = band
        self._rate = rate
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return self._id

    def assess(self, plan: EntryPlan, underlying_price: Decimal, as_of: datetime) -> GreekCheck:
        require_utc(as_of, "as_of")
        quote = plan.choice.quote
        expires_at = _expiry(quote, as_of, self._instrument)
        if isinstance(expires_at, _Refused):
            return self._check(plan, expires_at)
        early = self._before_valuing(quote, expires_at, as_of)
        if early is not None:
            return self._check(plan, early)
        valued = _value(quote, underlying_price, as_of, expires_at, self._band, self._rate)
        if isinstance(valued, _Refused):
            return self._check(plan, self._refusal(quote, valued))
        allowed, reason = self._judge(quote, valued)
        m = valued.model
        return GreekCheck(
            agent_id=self._id,
            allowed=allowed,
            reason=reason,
            contract_id=quote.contract_id,
            contracts=plan.contracts,
            expires_at=valued.expires_at,
            years=valued.years,
            implied_vol=m.vol,
            delta=m.delta,
            gamma=m.gamma,
            vega=m.vega,
        )

    def _check(self, plan: EntryPlan, refused: _Refused) -> GreekCheck:
        return GreekCheck(
            agent_id=self._id,
            allowed=False,
            reason=refused.reason,
            contract_id=plan.choice.quote.contract_id,
            contracts=plan.contracts,
            expires_at=refused.expires_at,
            years=refused.years,
            implied_vol=None,
            delta=None,
            gamma=None,
            vega=None,
        )

    def _before_valuing(
        self, quote: OptionQuote, expires_at: datetime, as_of: datetime
    ) -> _Refused | None:
        """A hard danger found from the expiry moment alone (Gamma's); None: go on."""
        return None

    def _refusal(self, quote: OptionQuote, refused: _Refused) -> _Refused:
        """How this agent words a valuation it could not make (Vega's own danger)."""
        return refused

    def _judge(self, quote: OptionQuote, valued: _Valued) -> tuple[bool, str]:
        raise NotImplementedError


class VegaAgent(_GreeksAgent):
    """No entry when no volatility inside the vega band prices the quote's mid."""

    _id = "vega"

    def _refusal(self, quote: OptionQuote, refused: _Refused) -> _Refused:
        exc = refused.out_of_band
        if exc is None:
            return refused
        bound = self._band.min_implied_vol if exc.side == "below" else self._band.max_implied_vol
        return _Refused(
            f"{quote.contract_id}: implied volatility of mid {quote.mid} is {exc.side} "
            f"the {bound} bound (hard danger)",
            expires_at=refused.expires_at,
            years=refused.years,
        )

    def _judge(self, quote: OptionQuote, valued: _Valued) -> tuple[bool, str]:
        band = self._band
        return True, (
            f"{quote.contract_id}: implied volatility {valued.model.vol:.4f} inside "
            f"[{band.min_implied_vol}, {band.max_implied_vol}]"
        )


class DeltaAgent(_GreeksAgent):
    """No entry when the option's absolute delta is outside the delta band."""

    _id = "delta"

    def __init__(
        self,
        settings: DeltaSettings,
        band: VegaSettings,
        rate: Decimal,
        instrument: InstrumentProfile,
    ) -> None:
        super().__init__(band, rate, instrument)
        self._s = settings

    def _judge(self, quote: OptionQuote, valued: _Valued) -> tuple[bool, str]:
        s = self._s
        delta = valued.model.delta
        inside = float(s.min_abs_delta) <= abs(delta) <= float(s.max_abs_delta)
        where = "inside" if inside else "outside"
        return inside, (
            f"{quote.contract_id}: delta {delta:.4f} is {where} the "
            f"[{s.min_abs_delta}, {s.max_abs_delta}] band of its size"
            + ("" if inside else " (hard danger)")
        )


class GammaAgent(_GreeksAgent):
    """No entry inside the danger zone near expiry: less than min_hours_to_expiry left."""

    _id = "gamma"

    def __init__(
        self,
        settings: GammaSettings,
        band: VegaSettings,
        rate: Decimal,
        instrument: InstrumentProfile,
    ) -> None:
        super().__init__(band, rate, instrument)
        self._s = settings
        self._minimum = timedelta(hours=float(settings.min_hours_to_expiry))

    def _before_valuing(
        self, quote: OptionQuote, expires_at: datetime, as_of: datetime
    ) -> _Refused | None:
        left = expires_at - as_of
        if left >= self._minimum:
            return None
        return _Refused(
            f"{quote.contract_id}: {left / _HOUR:.2f} hours to expiry at "
            f"{expires_at.isoformat()}, under the {self._s.min_hours_to_expiry}-hour "
            "minimum (the danger zone near expiry)",
            expires_at=expires_at,
            years=left / _YEAR,
        )

    def _judge(self, quote: OptionQuote, valued: _Valued) -> tuple[bool, str]:
        hours = valued.years * _YEAR / _HOUR
        return True, (
            f"{quote.contract_id}: {hours:.2f} hours to expiry, at least the "
            f"{self._s.min_hours_to_expiry}-hour minimum; gamma {valued.model.gamma:.6f}"
        )
