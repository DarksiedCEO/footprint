"""NB-01 (stop can never be widened) and NB-17 (position size within the cap), as
adversarial tests: each one tries to break the rule every way the owning agent allows."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from footprint.core.config import ExecutionProfile, InstrumentProfile, RiskProfile
from footprint.decision.stop import BidTick, StopKeeper

D = Decimal


def keeper(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> StopKeeper:
    return StopKeeper(risk, instrument, execution)


NOW = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def test_nb01_no_caller_can_widen_the_stop(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = keeper(risk, instrument, execution)
    original = booker.open("t", D(150), 1).price
    # The stop agent, the trail and trade management each ask for a wider stop.
    for source in ("stop", "trail", "trade_mgmt"):
        for wider in (original - D("0.25"), original - 10, D("0.25")):
            refusal = booker.request("t", wider, source)
            assert refusal is not None, (source, wider)
            assert booker.current("t").price == original
            assert booker.resting_order("t").trigger == original


def test_nb01_bypass_attempts_change_nothing(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = keeper(risk, instrument, execution)
    copy = booker.open("t", D(150), 1)
    original = copy.price
    replace(copy, price=D("0.25"))  # editing a copy
    with pytest.raises(ValueError, match="already"):
        booker.open("t", D(100), 1)  # re-opening with a lower entry
    with pytest.raises(FrozenInstanceError):
        copy.price = D("0.25")  # type: ignore[misc]
    assert booker.current("t").price == original
    assert booker.resting_order("t").trigger == original


def test_nb01_falling_bids_never_lower_a_trailing_stop(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = keeper(risk, instrument, execution)
    booker.open("t", D(150), 1)
    levels = []
    for bid in (160, 210, 260, 230, 205, 201, 180, 120, D("0.25"), 0):
        booker.on_tick("t", BidTick(D(bid), NOW), NOW)
        levels.append(booker.current("t").price)
    assert levels == sorted(levels), levels  # monotonically non-decreasing


def test_nb01_a_trail_never_drags_a_tightened_stop_down(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = keeper(risk, instrument, execution)
    booker.open("t", D(150), 1)
    assert booker.request("t", D(190), "trade_mgmt") is None
    booker.on_tick("t", BidTick(D("200.25"), NOW), NOW)  # trail wants 175.25
    assert booker.current("t").price == D(190)


def test_nb17_no_premium_ever_sizes_over_the_cap(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    from datetime import date

    from footprint.agents.contract_picker import ContractChoice
    from footprint.core.calendar import SessionKind
    from footprint.core.options import OptionQuote, Right
    from footprint.decision.sizing import EntryPlan, size

    ts = NOW
    for session, cap in (
        (SessionKind.PRIME, risk.position_cap),
        (SessionKind.OVERNIGHT, risk.overnight_position_cap),
    ):
        for bid_ticks in range(160, 1400, 7):  # 40.00 .. 349.75 points
            bid = D(bid_ticks) / 4
            for spread in (D(0), D("0.25"), D(2), D("12.5")):
                quote = OptionQuote(
                    "C", date(2026, 9, 16), D(20000), Right.CALL, bid, bid + spread, 999
                )
                plan = size(ContractChoice(quote, 1, ts), session, risk, instrument, execution)
                if isinstance(plan, EntryPlan):
                    assert plan.cost <= cap, (session, bid, spread, plan)
                    assert plan.limit_ceiling <= quote.mid + quote.spread / 4
                    assert plan.limit_floor <= plan.limit_ceiling
                    assert plan.cost == plan.contracts * (
                        plan.limit_ceiling * instrument.point_value + risk.fee_per_contract_per_side
                    )
