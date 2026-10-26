"""Felix (contract): picks the one option contract a signal will trade (G3).

Rule, all numbers from config:
- calls for a long, puts for a short (v1 only buys options);
- the nearest expiration with days-to-expiry between dte_min and dte_max, counted
  in calendar days from the TRADING day (an overnight session counts from the day
  it leads into);
- in that expiration, the strike nearest the underlying price (a tie goes to the
  in-the-money side: the lower strike for a call, the higher for a put), and no
  further than max_strike_distance from it;
- skip the trade (never fall back to another contract) when that contract has no
  bid, its spread is wider than max_spread_pct_of_mid of its mid, its spread is
  wider than max_spread_points option points (M3: both spread checks must pass; a
  spread equal to either cap passes), or its open interest is below
  min_open_interest. A chain older than quotes.max_age_seconds
  is not traded on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from footprint.core.calendar import trading_day_at
from footprint.core.config import ExecutionProfile, InstrumentProfile
from footprint.core.models import Direction, require_utc
from footprint.core.options import OptionChain, OptionQuote, Right


@dataclass(frozen=True, slots=True)
class ContractChoice:
    quote: OptionQuote
    dte: int
    chain_ts: datetime


@dataclass(frozen=True, slots=True)
class NoContract:
    reason: str


class ContractPicker:
    def __init__(self, execution: ExecutionProfile, instrument: InstrumentProfile) -> None:
        self._execution = execution
        self._instrument = instrument

    @property
    def agent_id(self) -> str:
        return "contract"

    def choose(
        self, chain: OptionChain, direction: Direction, as_of: datetime
    ) -> ContractChoice | NoContract:
        require_utc(as_of, "as_of")
        if chain.underlying != self._instrument.symbol:
            return NoContract(f"chain is for {chain.underlying!r}, not the traded instrument")
        age = as_of - chain.ts
        if age < timedelta(0):
            return NoContract(f"chain is stamped {-age} in the future")
        if age > timedelta(seconds=self._execution.quotes.max_age_seconds):
            return NoContract(f"chain is stale: {age} old")

        right = Right.for_direction(direction)
        today = trading_day_at(as_of, self._instrument)
        if today is None:
            return NoContract("market closed: no trading day to count expiry from")
        lo, hi = self._instrument.dte_min, self._instrument.dte_max
        eligible = [
            q for q in chain.quotes if q.right is right and lo <= (q.expiry - today).days <= hi
        ]
        if not eligible:
            return NoContract(f"no {right.value.lower()} expiring in {lo}-{hi} days")
        nearest_expiry = min(q.expiry for q in eligible)
        same_expiry = [q for q in eligible if q.expiry == nearest_expiry]
        price = chain.underlying_price

        def distance(q: OptionQuote) -> tuple[Decimal, Decimal]:
            in_the_money_first = q.strike if right is Right.CALL else -q.strike
            return (abs(q.strike - price), in_the_money_first)

        quote = min(same_expiry, key=distance)
        dte = (quote.expiry - today).days
        rules = self._execution.contract
        if abs(quote.strike - price) > rules.max_strike_distance:
            return NoContract(
                f"nearest strike {quote.strike} is {abs(quote.strike - price)} points from "
                f"{price}, over {rules.max_strike_distance}"
            )
        if quote.bid <= 0:
            return NoContract(f"{quote.contract_id}: no bid")
        spread_pct = (quote.spread / quote.mid).scaleb(2)
        if spread_pct > rules.max_spread_pct_of_mid:
            return NoContract(
                f"{quote.contract_id}: spread {spread_pct:.1f}% of mid "
                f"> {rules.max_spread_pct_of_mid}%"
            )
        if quote.spread > rules.max_spread_points:
            return NoContract(
                f"{quote.contract_id}: spread {quote.spread} points "
                f"> {rules.max_spread_points} points"
            )
        if quote.open_interest < rules.min_open_interest:
            return NoContract(
                f"{quote.contract_id}: open interest {quote.open_interest} "
                f"< {rules.min_open_interest}"
            )
        return ContractChoice(quote=quote, dte=dte, chain_ts=chain.ts)
