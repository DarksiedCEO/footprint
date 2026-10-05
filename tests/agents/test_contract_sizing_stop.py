"""Felix (contract, G3), Ezra (sizing, NB-17) and Booker (stop and trail, G1, G2, G7),
tested from the spec."""

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.contract_picker import ContractChoice, ContractPicker, NoContract
from footprint.core.calendar import SessionKind
from footprint.core.config import ExecutionProfile, InstrumentProfile, RiskProfile
from footprint.core.models import Direction
from footprint.core.options import OptionChain, OptionQuote, Right
from footprint.decision.sizing import EntryPlan, NoSize, size
from footprint.decision.stop import StopKeeper, StopLevel

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
TUE = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=PT).astimezone(UTC)


def q(
    expiry: date,
    strike: float,
    right: Right = Right.CALL,
    bid: float = 150,
    ask: float = 152,
    oi: int = 500,
) -> OptionQuote:
    return OptionQuote(
        contract_id=f"{right.value}-{expiry}-{strike}",
        expiry=expiry,
        strike=D(str(strike)),
        right=right,
        bid=D(str(bid)),
        ask=D(str(ask)),
        open_interest=oi,
    )


def chain(*quotes: OptionQuote, price: float = 20010, ts: datetime = NOW) -> OptionChain:
    return OptionChain(underlying="NQ", ts=ts, underlying_price=D(str(price)), quotes=quotes)


@pytest.fixture
def felix(execution: ExecutionProfile, instrument: InstrumentProfile) -> ContractPicker:
    return ContractPicker(execution, instrument)


def picked(result: ContractChoice | NoContract) -> OptionQuote:
    assert isinstance(result, ContractChoice), result
    return result.quote


# --- models ---------------------------------------------------------------------


def test_quotes_validate() -> None:
    with pytest.raises(ValueError, match="ask"):
        q(TUE, 20000, bid=10, ask=9)
    with pytest.raises(ValueError, match="ask"):
        q(TUE, 20000, bid=0, ask=0)
    with pytest.raises(ValueError, match="open_interest"):
        q(TUE, 20000, oi=-1)
    with pytest.raises(ValueError, match="duplicate"):
        chain(q(TUE, 20000), q(TUE, 20000))
    with pytest.raises(ValueError, match="UTC"):
        chain(q(TUE, 20000), ts=datetime(2026, 9, 15, 7, 0, tzinfo=PT))
    assert Right.for_direction(Direction.LONG) is Right.CALL
    assert Right.for_direction(Direction.SHORT) is Right.PUT


# --- Felix ----------------------------------------------------------------------


def test_felix_picks_calls_for_longs_and_puts_for_shorts(felix: ContractPicker) -> None:
    wed = TUE + timedelta(days=1)
    c = chain(q(wed, 20000, Right.CALL), q(wed, 20000, Right.PUT))
    assert picked(felix.choose(c, Direction.LONG, NOW)).right is Right.CALL
    assert picked(felix.choose(c, Direction.SHORT, NOW)).right is Right.PUT


def test_felix_takes_the_nearest_expiry_inside_one_to_three_days(felix: ContractPicker) -> None:
    c = chain(*(q(TUE + timedelta(days=d), 20000) for d in (0, 1, 2, 3, 4)))
    result = felix.choose(c, Direction.LONG, NOW)
    assert isinstance(result, ContractChoice)
    assert (result.quote.expiry, result.dte) == (TUE + timedelta(days=1), 1)
    only_out_of_range = chain(q(TUE, 20000), q(TUE + timedelta(days=4), 20000))
    assert isinstance(felix.choose(only_out_of_range, Direction.LONG, NOW), NoContract)


def test_felix_counts_calendar_days_over_a_weekend(felix: ContractPicker) -> None:
    fri = datetime(2026, 9, 18, 7, 0, tzinfo=PT).astimezone(UTC)
    mon = date(2026, 9, 21)
    result = felix.choose(chain(q(mon, 20000), ts=fri), Direction.LONG, fri)
    assert isinstance(result, ContractChoice)
    assert result.dte == 3


def test_felix_counts_days_in_pacific_time(felix: ContractPicker) -> None:
    # 22:30 PT Tuesday is already Wednesday in UTC; Wednesday expiry is still 1 day out.
    late = datetime(2026, 9, 15, 22, 30, tzinfo=PT).astimezone(UTC)
    wed = TUE + timedelta(days=1)
    result = felix.choose(chain(q(wed, 20000), ts=late), Direction.LONG, late)
    assert isinstance(result, ContractChoice)
    assert result.dte == 1


@pytest.mark.parametrize(
    ("price", "direction", "strike"),
    [
        (20010, Direction.LONG, 20000),
        (20015, Direction.LONG, 20025),
        (20012.5, Direction.LONG, 20000),  # tie: the in-the-money call (lower strike)
        (20012.5, Direction.SHORT, 20025),  # tie: the in-the-money put (higher strike)
    ],
)
def test_felix_takes_the_strike_nearest_the_money(
    felix: ContractPicker, price: float, direction: Direction, strike: int
) -> None:
    wed = TUE + timedelta(days=1)
    right = Right.for_direction(direction)
    c = chain(*(q(wed, k, right) for k in (19975, 20000, 20025, 20050)), price=price)
    assert picked(felix.choose(c, direction, NOW)).strike == D(strike)


@pytest.mark.parametrize(
    ("bid", "ask", "oi", "ok"),
    [
        (97.5, 102.5, 500, True),  # spread exactly 5% of mid
        (97.25, 102.5, 500, False),  # 5.26%
        (0, 2, 500, False),  # no bid
        (150, 152, 100, True),  # open interest exactly at the floor
        (150, 152, 99, False),
    ],
)
def test_felix_skips_illiquid_contracts(
    felix: ContractPicker, bid: float, ask: float, oi: int, ok: bool
) -> None:
    c = chain(q(TUE + timedelta(days=1), 20000, bid=bid, ask=ask, oi=oi))
    assert isinstance(felix.choose(c, Direction.LONG, NOW), ContractChoice) is ok


def test_felix_never_falls_back_to_another_contract(felix: ContractPicker) -> None:
    wed = TUE + timedelta(days=1)
    c = chain(q(wed, 20000, bid=50, ask=150), q(wed, 20025), q(TUE + timedelta(days=2), 20000))
    result = felix.choose(c, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert "spread" in result.reason


def test_felix_refuses_stale_future_or_foreign_chains(felix: ContractPicker) -> None:
    wed = TUE + timedelta(days=1)
    fresh = chain(q(wed, 20000), ts=NOW - timedelta(seconds=5))
    assert isinstance(felix.choose(fresh, Direction.LONG, NOW), ContractChoice)
    stale = chain(q(wed, 20000), ts=NOW - timedelta(seconds=6))
    assert "stale" in felix.choose(stale, Direction.LONG, NOW).reason  # type: ignore[union-attr]
    future = chain(q(wed, 20000), ts=NOW + timedelta(seconds=1))
    assert "future" in felix.choose(future, Direction.LONG, NOW).reason  # type: ignore[union-attr]
    foreign = OptionChain("ES", NOW, D(6000), (q(wed, 6000),))
    assert isinstance(felix.choose(foreign, Direction.LONG, NOW), NoContract)


# --- Ezra -----------------------------------------------------------------------


def choice(bid: float, ask: float) -> ContractChoice:
    return ContractChoice(
        quote=q(TUE + timedelta(days=1), 20000, bid=bid, ask=ask), dte=1, chain_ts=NOW
    )


def sized(
    bid: float,
    ask: float,
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
    session: SessionKind = SessionKind.PRIME,
) -> EntryPlan | NoSize:
    return size(choice(bid, ask), session, risk, instrument, execution)


def test_ezra_sizes_against_the_worst_allowed_price(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    plan = sized(150, 152, risk, instrument, execution)
    assert isinstance(plan, EntryPlan)
    # mid 151 + 25% of the 2-point spread = 151.5; x $20 + $2.50 fee
    assert plan.limit_ceiling == D("151.5")
    assert plan.contracts == 1
    assert plan.cost == D("3032.5")


def test_ezra_rounds_the_ceiling_up_to_the_tick_and_never_past_the_ask(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    plan = sized(150, 151.25, risk, instrument, execution)  # 150.9375 -> 151.00
    assert isinstance(plan, EntryPlan)
    assert plan.limit_ceiling == D("151.00")
    locked = sized(151, 151, risk, instrument, execution)
    assert isinstance(locked, EntryPlan)
    assert locked.limit_ceiling == D(151)


@pytest.mark.parametrize(
    ("bid", "ask", "fits"),
    [
        (249.75, 249.75, True),  # 249.75 x 20 + 2.50 = 4997.50
        (250, 250, False),  # 5002.50: over the 5000 cap
    ],
)
def test_ezra_never_exceeds_the_position_cap(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
    bid: float,
    ask: float,
    fits: bool,
) -> None:
    plan = sized(bid, ask, risk, instrument, execution)
    assert isinstance(plan, EntryPlan) is fits
    if isinstance(plan, EntryPlan):
        assert plan.cost <= risk.position_cap


def test_ezra_uses_the_session_cap_and_refuses_closed(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    assert isinstance(sized(150, 152, risk, instrument, execution, SessionKind.OVERNIGHT), NoSize)
    assert isinstance(
        sized(100, 102, risk, instrument, execution, SessionKind.OVERNIGHT), EntryPlan
    )
    assert isinstance(sized(150, 152, risk, instrument, execution, SessionKind.CLOSED), NoSize)


def test_ezra_refuses_a_premium_too_small_for_the_hard_stop(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    # $750 / $20 = 37.5 points of stop; the mid must stay more than one tick above it.
    assert isinstance(sized(37.5, 38, risk, instrument, execution), NoSize)
    assert isinstance(sized(39.75, 40.25, risk, instrument, execution), EntryPlan)


def test_ezra_caps_contracts_at_max_contracts(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    plan = sized(50, 50, risk, instrument, execution)  # three would fit under the cap
    assert isinstance(plan, EntryPlan)
    assert plan.contracts == risk.max_contracts == 1


# --- Booker ---------------------------------------------------------------------


@pytest.fixture
def booker(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> StopKeeper:
    return StopKeeper(risk, instrument, execution)


def test_hard_stop_is_stop_loss_below_the_fill(booker: StopKeeper) -> None:
    stop = booker.initial(D("151.5"), 1)
    assert stop.price == D("114.0")  # 151.5 - 750/20
    assert not stop.trailing


def test_hard_stop_rounds_toward_a_smaller_loss(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    odd = StopKeeper(replace(risk, stop_loss=D(752)), instrument, execution)
    stop = odd.initial(D("151.5"), 1)  # 151.5 - 37.6 = 113.9 -> up to 114.0
    assert stop.price == D("114.0")
    for entry in (D("100.25"), D("151.75"), D("333.5")):
        loss = (entry - odd.initial(entry, 1).price) * instrument.point_value
        assert loss <= D(752)


def test_hard_stop_refuses_a_premium_it_cannot_protect(booker: StopKeeper) -> None:
    with pytest.raises(ValueError, match="too small"):
        booker.initial(D("37.5"), 1)


def test_trail_engages_at_the_profit_target_and_follows_half_the_peak(
    booker: StopKeeper,
) -> None:
    stop = booker.initial(D(150), 1)  # hard stop 112.5
    stop = booker.on_bid(stop, D("199.75"))  # +$995: not yet
    assert (stop.price, stop.trailing) == (D("112.5"), False)
    stop = booker.on_bid(stop, D(200))  # +$1,000: lock entry + $500
    assert (stop.price, stop.trailing) == (D(175), True)
    stop = booker.on_bid(stop, D(220))  # +$1,400: keep $700
    assert stop.price == D(185)
    stop = booker.on_bid(stop, D(190))  # pullback: the stop holds
    assert stop.price == D(185)
    assert stop.peak_bid == D(220)
    assert not booker.hit(stop, D("185.25"))
    assert booker.hit(stop, D(185))


def test_trail_rounds_down_to_the_tick(booker: StopKeeper) -> None:
    stop = booker.on_bid(booker.initial(D(150), 1), D("200.25"))  # keep $502.50 -> 25.125 pts
    assert stop.price == D(175)


def test_resting_stop_limit_and_gap_watchdog(booker: StopKeeper) -> None:
    stop = booker.initial(D("151.5"), 1)
    order = booker.resting_order(stop)
    assert (order.trigger, order.limit) == (D(114), D(109))  # 20 ticks of 0.25 below
    assert booker.gapped_through(stop, D("108.75"))
    assert not booker.gapped_through(stop, D(109))
    low = StopLevel(entry=D(40), contracts=1, price=D(2), peak_bid=D(40), trailing=False)
    assert booker.resting_order(low).limit == D("0.25")  # never below one tick


def test_stop_level_validates() -> None:
    with pytest.raises(ValueError, match="price"):
        StopLevel(entry=D(1), contracts=1, price=D(0), peak_bid=D(1), trailing=False)
    with pytest.raises(ValueError, match="contracts"):
        StopLevel(entry=D(1), contracts=0, price=D(1), peak_bid=D(1), trailing=False)


def test_ratchet_logs_refusals(booker: StopKeeper, caplog: pytest.LogCaptureFixture) -> None:
    stop = booker.initial(D(150), 1)
    with caplog.at_level(logging.WARNING, logger="footprint.stop"):
        same, refusal = booker.ratchet(stop, D(100), "trade_mgmt")
    assert same == stop
    assert refusal is not None
    assert (refusal.requested, refusal.kept, refusal.source) == (D(100), D("112.5"), "trade_mgmt")
    assert "refused" in caplog.text
    tighter, none = booker.ratchet(stop, D(120), "trade_mgmt")
    assert (tighter.price, none) == (D(120), None)


def test_felix_names_a_missing_bid(felix: ContractPicker) -> None:
    result = felix.choose(
        chain(q(TUE + timedelta(days=1), 20000, bid=0, ask=2)), Direction.LONG, NOW
    )
    assert isinstance(result, NoContract)
    assert "no bid" in result.reason
