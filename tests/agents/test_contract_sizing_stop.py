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
from footprint.core.config import ConfigError, ExecutionProfile, InstrumentProfile, RiskProfile
from footprint.core.models import Direction
from footprint.core.options import OptionChain, OptionQuote, Right
from footprint.decision.sizing import EntryPlan, NoSize, size
from footprint.decision.stop import BidTick, StopKeeper, TickStatus

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


def test_felix_counts_from_the_pacific_trading_day(felix: ContractPicker) -> None:
    # 22:30 PT Tuesday is Wednesday in UTC and belongs to Wednesday's trading day, so a
    # Wednesday expiry is 0 days out (excluded) and Thursday's is 1.
    late = datetime(2026, 9, 15, 22, 30, tzinfo=PT).astimezone(UTC)
    wed, thu = TUE + timedelta(days=1), TUE + timedelta(days=2)
    result = felix.choose(chain(q(wed, 20000), q(thu, 20000), ts=late), Direction.LONG, late)
    assert isinstance(result, ContractChoice)
    assert (result.quote.expiry, result.dte) == (thu, 1)


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


def test_felix_names_a_missing_bid(felix: ContractPicker) -> None:
    c = chain(q(TUE + timedelta(days=1), 20000, bid=0, ask=2))
    result = felix.choose(c, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert "no bid" in result.reason


def test_felix_counts_from_the_trading_day_overnight(felix: ContractPicker) -> None:
    # Thursday 17:00 PT is Friday's overnight session: a Friday expiry is 0 days out.
    thu_evening = datetime(2026, 10, 8, 17, 0, tzinfo=PT).astimezone(UTC)
    fri, mon = date(2026, 10, 9), date(2026, 10, 12)
    c = chain(q(fri, 20000), q(mon, 20000), ts=thu_evening)
    result = felix.choose(c, Direction.LONG, thu_evening)
    assert isinstance(result, ContractChoice)
    assert (result.quote.expiry, result.dte) == (mon, 3)
    closed = datetime(2026, 10, 10, 9, 0, tzinfo=PT).astimezone(UTC)  # Saturday
    assert isinstance(
        felix.choose(chain(q(mon, 20000), ts=closed), Direction.LONG, closed), NoContract
    )


def test_felix_refuses_a_far_strike_in_a_gappy_chain(felix: ContractPicker) -> None:
    wed = TUE + timedelta(days=1)
    gappy = chain(q(wed, 19500), q(wed, 20500), price=20010)
    result = felix.choose(gappy, Direction.LONG, NOW)
    assert isinstance(result, NoContract)
    assert "points from" in result.reason
    near = chain(q(wed, 19960), price=20010)  # exactly 50 points away
    assert isinstance(felix.choose(near, Direction.LONG, NOW), ContractChoice)


# --- Ezra -----------------------------------------------------------------------


def choice(bid: float, ask: float) -> ContractChoice:
    quote = q(TUE + timedelta(days=1), 20000, bid=bid, ask=ask)
    return ContractChoice(quote=quote, dte=1, chain_ts=NOW)


def sized(
    bid: float,
    ask: float,
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
    session: SessionKind = SessionKind.PRIME,
) -> EntryPlan | NoSize:
    return size(choice(bid, ask), session, risk, instrument, execution)


@pytest.mark.parametrize(
    ("bid", "ask", "floor", "ceiling"),
    [
        (150, 152, "151", "151.5"),  # mid 151 + 25% of 2 = 151.5
        (150, 151.25, "150.5", "150.75"),  # 150.9375 rounds DOWN to the tick
        (149.75, 150, "149.75", "149.75"),  # one-tick spread: never above the bid
        (151, 151, "151", "151"),
    ],
)
def test_ezra_prices_stay_on_the_tick_and_inside_g5(
    risk: RiskProfile,
    instrument: InstrumentProfile,
    execution: ExecutionProfile,
    bid: float,
    ask: float,
    floor: str,
    ceiling: str,
) -> None:
    plan = sized(bid, ask, risk, instrument, execution)
    assert isinstance(plan, EntryPlan)
    assert (plan.limit_floor, plan.limit_ceiling) == (D(floor), D(ceiling))
    quote = plan.choice.quote
    assert plan.limit_ceiling <= quote.mid + quote.spread * D("0.25")


def test_ezra_cost_includes_the_entry_fee(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    plan = sized(150, 152, risk, instrument, execution)
    assert isinstance(plan, EntryPlan)
    assert plan.cost == D("3032.5")  # 151.5 x $20 + $2.50


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


def test_ezra_sizes_prime_only(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    for session in (SessionKind.OVERNIGHT, SessionKind.CLOSED):
        result = sized(100, 102, risk, instrument, execution, session)
        assert isinstance(result, NoSize)
        assert "prime" in result.reason


def test_ezra_refuses_a_premium_too_small_for_the_stop_and_its_limit(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    # Stop 37.5 points below the fill, limit 5 points below that, must stay >= 0.25.
    assert isinstance(sized(42.5, 43, risk, instrument, execution), EntryPlan)  # floor 42.75
    assert isinstance(sized(42.25, 42.75, risk, instrument, execution), NoSize)  # floor 42.5


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


def tick(bid: float, age_seconds: float = 0) -> BidTick:
    return BidTick(D(str(bid)), NOW - timedelta(seconds=age_seconds))


def test_hard_stop_is_stop_loss_below_the_fill(booker: StopKeeper) -> None:
    stop = booker.open("t1", D("151.5"), 1)
    assert stop.price == D("114.0")  # 151.5 - 750/20
    assert not stop.trailing
    assert booker.current("t1") == stop


def test_hard_stop_rounds_toward_a_smaller_loss(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    odd = StopKeeper(replace(risk, stop_loss=D(752)), instrument, execution)
    assert odd.open("a", D("151.5"), 1).price == D("114.0")  # 113.9 rounds up
    for i, entry in enumerate((D("100.25"), D("151.75"), D("333.5"))):
        loss = (entry - odd.open(f"p{i}", entry, 1).price) * instrument.point_value
        assert loss <= D(752)


def test_open_validates(booker: StopKeeper) -> None:
    booker.open("t1", D(150), 1)
    with pytest.raises(ValueError, match="already"):
        booker.open("t1", D(150), 1)
    with pytest.raises(ValueError, match="tick"):
        booker.open("t2", D("150.1"), 1)
    with pytest.raises(ValueError, match="too small"):
        booker.open("t3", D("42.5"), 1)
    booker.open("t4", D("42.75"), 1)
    with pytest.raises(ValueError, match="contracts"):
        booker.open("t5", D(150), 0)


def test_trail_engages_at_the_net_profit_target_and_follows_half_the_net_peak(
    booker: StopKeeper,
) -> None:
    booker.open("t", D(150), 1)  # hard stop 112.5; round-trip fees $5
    booker.on_tick("t", tick(200), NOW)  # +$1,000 gross, $995 net: not yet
    assert (booker.current("t").price, booker.current("t").trailing) == (D("112.5"), False)
    booker.on_tick("t", tick(200.25), NOW)  # $1,000 net: lock $500 net
    assert booker.current("t").price == D("175.25")  # 150 + (500 + 5) / 20
    assert booker.current("t").trailing
    booker.on_tick("t", tick(220), NOW)  # $1,395 net: keep $697.50 -> 185.125 -> up
    assert booker.current("t").price == D("185.25")
    booker.on_tick("t", tick(190), NOW)  # pullback: the stop holds
    assert booker.current("t").price == D("185.25")
    assert booker.current("t").peak_bid == D(220)
    assert not booker.hit("t", tick(185.5), NOW)
    assert booker.hit("t", tick(185.25), NOW)


def test_locked_profit_is_at_least_the_keep_share_net_of_fees(
    booker: StopKeeper, risk: RiskProfile, instrument: InstrumentProfile
) -> None:
    booker.open("t", D(150), 1)
    for peak in (D("200.25"), D("213.75"), D("250.5"), D(400)):
        booker.on_tick("t", BidTick(peak, NOW), NOW)
        stop = booker.current("t")
        fees = 2 * risk.fee_per_contract_per_side
        net_locked = (stop.price - stop.entry) * instrument.point_value - fees
        net_peak = (peak - stop.entry) * instrument.point_value - fees
        assert net_locked >= net_peak / 2


def test_stale_and_no_bid_ticks_never_move_or_trigger(booker: StopKeeper) -> None:
    booker.open("t", D(150), 1)
    assert booker.on_tick("t", tick(260, age_seconds=6), NOW) is TickStatus.STALE
    assert booker.on_tick("t", BidTick(D(260), NOW + timedelta(seconds=1)), NOW) is TickStatus.STALE
    assert booker.current("t").peak_bid == D(150)
    assert not booker.hit("t", tick(100, age_seconds=6), NOW)
    assert booker.on_tick("t", tick(0), NOW) is TickStatus.NO_BID
    assert not booker.hit("t", tick(0), NOW)
    assert not booker.gapped_through("t", tick(0), NOW)
    assert booker.on_tick("t", tick(151, age_seconds=5), NOW) is TickStatus.USED


def test_resting_stop_limit_and_gap_watchdog(booker: StopKeeper) -> None:
    booker.open("t", D("151.5"), 1)
    order = booker.resting_order("t")
    assert (order.trigger, order.limit) == (D(114), D(109))  # 20 ticks of 0.25 below
    assert booker.gapped_through("t", tick(108.75), NOW)
    assert not booker.gapped_through("t", tick(109), NOW)


def test_requests_are_ratcheted_on_the_tick_and_logged(
    booker: StopKeeper, caplog: pytest.LogCaptureFixture
) -> None:
    booker.open("t", D(150), 1)
    with caplog.at_level(logging.WARNING, logger="footprint.stop"):
        refusal = booker.request("t", D(100), "trade_mgmt")
    assert refusal is not None
    assert (refusal.requested, refusal.kept, refusal.source) == (D(100), D("112.5"), "trade_mgmt")
    assert "widen" in refusal.reason
    assert "refused" in caplog.text
    assert booker.request("t", D("120.1"), "trade_mgmt") is not None  # off the tick
    assert booker.request("t", D("NaN"), "trade_mgmt") is not None
    assert booker.current("t").price == D("112.5")
    assert booker.request("t", D(120), "trade_mgmt") is None
    assert booker.current("t").price == D(120)


def test_copies_cannot_change_the_stop(booker: StopKeeper) -> None:
    copy = booker.open("t", D(150), 1)
    widened = replace(copy, price=D("0.25"))
    assert widened.price == D("0.25")
    assert booker.current("t").price == D("112.5")
    assert booker.resting_order("t").trigger == D("112.5")
    assert booker.close("t").price == D("112.5")
    with pytest.raises(KeyError):
        booker.current("t")


def test_keeper_refuses_a_limit_offset_that_breaks_stop_max(
    risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
) -> None:
    wide = replace(execution, stop_order=replace(execution.stop_order, limit_offset_ticks=50))
    # 750 + 50 x 0.25 x 20 + 5 = 1005 > 1000
    with pytest.raises(ConfigError, match="stop_max"):
        StopKeeper(risk, instrument, wide)
    ok = replace(execution, stop_order=replace(execution.stop_order, limit_offset_ticks=49))
    StopKeeper(risk, instrument, ok)  # 750 + 245 + 5 = 1000


def test_bid_tick_validates() -> None:
    with pytest.raises(ValueError, match="bid"):
        BidTick(D(-1), NOW)
    with pytest.raises(ValueError, match="UTC"):
        BidTick(D(1), datetime(2026, 9, 15, 7, 0, tzinfo=PT))
