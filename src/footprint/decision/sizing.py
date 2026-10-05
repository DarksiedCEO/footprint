"""Ezra (sizing): how many contracts, at what worst-case cost (NB-17).

The cost Ezra sizes against is the most Diego could ever pay: the mid plus the
configured share of the spread, rounded UP to the option tick and never above the
ask, times the point value, plus the entry fee. So the position cap holds even if
the entry fills at the worst allowed price. Fewer contracts if the cap requires;
skip the trade if not even one fits. v1 trades at most max_contracts (one).

Ezra also refuses a premium too small to carry the hard stop: if the stop price
would land at or below one option tick, the stop cannot be placed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from footprint.agents.contract_picker import ContractChoice
from footprint.core.calendar import SessionKind
from footprint.core.config import ExecutionProfile, InstrumentProfile, RiskProfile


@dataclass(frozen=True, slots=True)
class EntryPlan:
    choice: ContractChoice
    contracts: int
    limit_ceiling: Decimal  # highest premium Diego may pay, in points
    cost: Decimal  # contracts x (limit_ceiling x point value + entry fee)


@dataclass(frozen=True, slots=True)
class NoSize:
    reason: str


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    steps = (price / tick).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR)
    return steps * tick


def size(
    choice: ContractChoice,
    session: SessionKind,
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
) -> EntryPlan | NoSize:
    if session is SessionKind.CLOSED:
        return NoSize("session is closed")
    cap = risk.position_cap if session is SessionKind.PRIME else risk.overnight_position_cap
    quote = choice.quote
    tick = instrument.option_tick_size
    share = execution.entry.max_over_mid_pct_of_spread.scaleb(-2)
    ceiling = min(round_to_tick(quote.mid + quote.spread * share, tick, up=True), quote.ask)
    per_contract = ceiling * instrument.point_value + risk.fee_per_contract_per_side
    contracts = min(risk.max_contracts, int(cap // per_contract))
    if contracts < 1:
        return NoSize(f"one contract costs up to {per_contract}, over the {cap} cap")
    stop_points = risk.stop_loss / (instrument.point_value * contracts)
    if quote.mid - stop_points <= tick:  # Diego never fills below the mid
        return NoSize(f"premium {quote.mid} is too small to carry a {risk.stop_loss} hard stop")
    return EntryPlan(
        choice=choice,
        contracts=contracts,
        limit_ceiling=ceiling,
        cost=per_contract * contracts,
    )
