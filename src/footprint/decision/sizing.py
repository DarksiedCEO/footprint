"""Ezra (sizing): how many contracts, at what worst-case cost (NB-17).

Prices Diego may use are on the option tick:
- `limit_floor`: the mid rounded DOWN to the tick, where Diego starts;
- `limit_ceiling`: mid + max_over_mid_pct_of_spread of the spread, rounded DOWN to
  the tick (G5: never past that point; with a one-tick spread this is the bid).
Ezra sizes against the ceiling plus the entry fee, so the position cap holds at
the worst allowed fill. Fewer contracts if the cap requires; skip if not even one
fits. v1 trades at most max_contracts (one) and only in the prime session: the
overnight higher bar is not built, so overnight entries are refused here as well.

Ezra also refuses a premium too small to carry the hard stop with its full resting
limit width (G1): the stop for a fill at the floor, minus the limit offset, must
stay at or above one tick. Booker applies the same rule to the actual fill.
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
    limit_floor: Decimal  # first price Diego posts, in points
    limit_ceiling: Decimal  # highest premium Diego may pay, in points
    cost: Decimal  # contracts x (limit_ceiling x point value + entry fee)


@dataclass(frozen=True, slots=True)
class NoSize:
    reason: str


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    steps = (price / tick).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR)
    return steps * tick


def hard_stop_price(
    fill: Decimal, contracts: int, risk: RiskProfile, instrument: InstrumentProfile
) -> Decimal:
    """Fill minus stop_loss of option value, rounded UP (toward a smaller loss)."""
    points = risk.stop_loss / (instrument.point_value * contracts)
    return round_to_tick(fill - points, instrument.option_tick_size, up=True)


def stop_fits(
    fill: Decimal,
    contracts: int,
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
) -> bool:
    """The hard stop for `fill` keeps its full resting-limit width above one tick."""
    tick = instrument.option_tick_size
    stop = hard_stop_price(fill, contracts, risk, instrument)
    return stop - tick * execution.stop_order.limit_offset_ticks >= tick


def size(
    choice: ContractChoice,
    session: SessionKind,
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
) -> EntryPlan | NoSize:
    if session is not SessionKind.PRIME:
        return NoSize(f"{session.value.lower()}: v1 sizes prime-session entries only")
    cap = risk.position_cap
    quote = choice.quote
    tick = instrument.option_tick_size
    share = execution.entry.max_over_mid_pct_of_spread.scaleb(-2)
    floor = round_to_tick(quote.mid, tick, up=False)
    ceiling = round_to_tick(quote.mid + quote.spread * share, tick, up=False)
    per_contract = ceiling * instrument.point_value + risk.fee_per_contract_per_side
    contracts = min(risk.max_contracts, int(cap // per_contract))
    if contracts < 1:
        return NoSize(f"one contract costs up to {per_contract}, over the {cap} cap")
    if not stop_fits(floor, contracts, risk, instrument, execution):
        return NoSize(f"premium {floor} is too small to carry the hard stop and its limit")
    return EntryPlan(
        choice=choice,
        contracts=contracts,
        limit_floor=floor,
        limit_ceiling=ceiling,
        cost=per_contract * contracts,
    )
