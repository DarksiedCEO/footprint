"""NB-01 (stop can never be widened) and NB-17 (position size within the cap), as
adversarial tests: each one tries to break the rule every way the owning agent allows."""

from decimal import Decimal

from footprint.core.config import ExecutionProfile, InstrumentProfile, RiskProfile
from footprint.decision.stop import StopKeeper

D = Decimal


def test_nb01_no_caller_can_widen_the_stop(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = StopKeeper(risk, instrument, execution)
    stop = booker.initial(D(150), 1)
    original = stop.price
    # The stop agent, the trail and trade management each ask for a wider stop.
    for source in ("stop", "trail", "trade_mgmt"):
        for wider in (original - D("0.25"), original - 10, D("0.25")):
            stop, refusal = booker.ratchet(stop, wider, source)
            assert refusal is not None, (source, wider)
            assert stop.price == original


def test_nb01_falling_bids_never_lower_a_trailing_stop(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = StopKeeper(risk, instrument, execution)
    stop = booker.initial(D(150), 1)
    prices = [D(p) for p in (160, 210, 260, 230, 205, 201, 180, 120, D("0.25"))]
    levels = []
    for bid in prices:
        stop = booker.on_bid(stop, bid)
        levels.append(stop.price)
    assert levels == sorted(levels), levels  # monotonically non-decreasing


def test_nb01_a_trail_never_drags_a_tightened_stop_down(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    booker = StopKeeper(risk, instrument, execution)
    stop, _ = booker.ratchet(booker.initial(D(150), 1), D(190), "trade_mgmt")
    stop = booker.on_bid(stop, D(200))  # trail would want entry + $500 = 175
    assert stop.price == D(190)


def test_nb17_no_premium_ever_sizes_over_the_cap(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    from datetime import UTC, date, datetime

    from footprint.agents.contract_picker import ContractChoice
    from footprint.core.calendar import SessionKind
    from footprint.core.options import OptionQuote, Right
    from footprint.decision.sizing import EntryPlan, size

    ts = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
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
                    worst = plan.contracts * (
                        quote.ask * instrument.point_value + risk.fee_per_contract_per_side
                    )
                    assert plan.limit_ceiling <= quote.ask
                    assert plan.cost <= worst
