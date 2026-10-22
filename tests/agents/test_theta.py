"""Theta (decay veto, decision B76-1 and B76-2), tested from the rule:

- the option is valued with Black-76 at the implied volatility its quote's mid implies,
  now and at the end of the planned hold, on a calendar clock to its expiry moment;
- the difference, for the sized contracts, is the modelled decay; above the budget
  (max_decay_pct_of_target of profit_target) there is no entry;
- hard dangers stop the entry outright: no volatility fits the quote, the volatility is
  outside the configured bounds, the option expires within the hold (or has expired),
  or the expiry is a quarterly (American) one.

Expected values are computed here from the certified Black-76 module, never copied from
the code under test."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.contract_picker import ContractChoice
from footprint.agents.theta import ThetaAgent, ThetaCheck
from footprint.core.black76 import Right76, price
from footprint.core.config import InstrumentProfile, RiskProfile, StrategyProfile, ThetaSettings
from footprint.core.expiry import expiry_moment, is_american, is_european_class
from footprint.core.implied_vol import implied_vol
from footprint.core.options import OptionQuote, Right
from footprint.decision.sizing import EntryPlan

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
YEAR = timedelta(days=365)
TUE = date(2026, 9, 15)
WED = date(2026, 9, 16)
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=PT).astimezone(UTC)
FORWARD = D(20010)


def quote(
    expiry: date = WED,
    strike: float = 20000,
    right: Right = Right.CALL,
    bid: float = 150,
    ask: float = 152,
    trading_class: str = "Q3C",  # a daily (European) class
) -> OptionQuote:
    return OptionQuote(
        contract_id=f"{right.value}-{expiry}-{strike}",
        expiry=expiry,
        strike=D(str(strike)),
        right=right,
        bid=D(str(bid)),
        ask=D(str(ask)),
        open_interest=500,
        trading_class=trading_class,
    )


def plan(q: OptionQuote, contracts: int = 1) -> EntryPlan:
    choice = ContractChoice(quote=q, dte=(q.expiry - TUE).days, chain_ts=NOW)
    return EntryPlan(
        choice=choice,
        contracts=contracts,
        limit_floor=q.mid,
        limit_ceiling=q.ask,
        cost=q.ask * 20 * contracts,
    )


@pytest.fixture
def settings(strategy: StrategyProfile) -> ThetaSettings:
    return strategy.theta


@pytest.fixture
def theta(settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile) -> ThetaAgent:
    return ThetaAgent(settings, instrument, risk)


def expected(
    q: OptionQuote,
    as_of: datetime,
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    forward: Decimal = FORWARD,
) -> tuple[float, float, float]:
    """(years, implied vol, decay points), worked out independently of the agent."""
    moment = expiry_moment(q.expiry, instrument)
    years = (moment - as_of) / YEAR
    hold = timedelta(minutes=settings.planned_hold_minutes) / YEAR
    right = Right76.CALL if q.right is Right.CALL else Right76.PUT
    rate = float(settings.rate)
    vol = implied_vol(
        right,
        forward=float(forward),
        strike=float(q.strike),
        years=years,
        rate=rate,
        price=float(q.mid),
        low=float(settings.min_implied_vol),
        high=float(settings.max_implied_vol),
    )
    args = {"forward": float(forward), "strike": float(q.strike), "vol": vol, "rate": rate}
    decay = price(right, years=years, **args) - price(right, years=years - hold, **args)
    return years, vol, decay


# --- the expiry moment and the exercise style (instrument config, CME 359A) --------


def test_an_option_expires_at_the_configured_time_on_its_day(instrument: InstrumentProfile) -> None:
    moment = expiry_moment(WED, instrument)
    assert moment.tzinfo is UTC
    assert moment.astimezone(PT) == datetime(2026, 9, 16, 13, 0, tzinfo=PT)


def test_on_a_stock_market_half_day_the_option_expires_early(
    instrument: InstrumentProfile,
) -> None:
    day = date(2026, 11, 27)  # the day after Thanksgiving
    rules = instrument.option_expiry
    assert day in rules.early_close_dates
    moment = expiry_moment(day, instrument)
    assert moment.tzinfo is UTC
    assert moment.astimezone(PT) == datetime.combine(day, rules.early_close_time, PT)


def test_a_futures_halt_caps_option_expiry(instrument: InstrumentProfile) -> None:
    day = date(2026, 9, 7)  # Labor Day: a futures early halt, not a stock half day
    assert day in instrument.early_closes
    assert day not in instrument.option_expiry.early_close_dates
    moment = expiry_moment(day, instrument)
    assert moment.tzinfo is UTC
    assert moment.astimezone(PT).time() == instrument.early_closes[day]  # not 13:00


def test_the_futures_halt_is_read_in_the_prime_session_timezone(
    instrument: InstrumentProfile,
) -> None:
    day = date(2026, 9, 7)  # Labor Day halt, 10:00 in the prime session's zone (PT)
    chicago = replace(instrument.option_expiry, timezone="America/Chicago")
    moment = expiry_moment(day, replace(instrument, option_expiry=chicago))
    assert moment.astimezone(PT) == datetime.combine(day, instrument.early_closes[day], PT)


def test_a_later_futures_halt_never_makes_option_expiry_later(
    instrument: InstrumentProfile,
) -> None:
    day = date(2026, 11, 27)  # a half day: options stop at the rule's early close
    later_halt = {**instrument.early_closes, day: time(10, 15)}  # e.g. 12:15 CT
    moment = expiry_moment(day, replace(instrument, early_closes=later_halt))
    assert moment.astimezone(PT).time() == instrument.option_expiry.early_close_time


def test_an_early_close_after_the_normal_time_never_makes_expiry_later(
    instrument: InstrumentProfile,
) -> None:
    day = date(2026, 11, 27)
    late = replace(instrument.option_expiry, early_close_time=time(14, 0))
    no_halt = {d: t for d, t in instrument.early_closes.items() if d != day}
    moment = expiry_moment(day, replace(instrument, option_expiry=late, early_closes=no_halt))
    assert moment.astimezone(PT).time() == instrument.option_expiry.time


def test_the_early_close_is_read_in_the_rules_timezone(instrument: InstrumentProfile) -> None:
    day = date(2026, 11, 27)
    chicago = replace(instrument.option_expiry, timezone="America/Chicago")
    moment = expiry_moment(day, replace(instrument, option_expiry=chicago))
    assert moment.tzinfo is UTC
    local = moment.astimezone(ZoneInfo("America/Chicago"))
    assert (local.date(), local.time()) == (day, instrument.option_expiry.early_close_time)


def test_the_expiry_clock_uses_its_own_timezone(instrument: InstrumentProfile) -> None:
    chicago = replace(
        instrument,
        option_expiry=replace(instrument.option_expiry, timezone="America/Chicago"),
    )
    moment = expiry_moment(WED, chicago)
    assert moment.astimezone(ZoneInfo("America/Chicago")).hour == instrument.option_expiry.time.hour


@pytest.mark.parametrize(
    ("trading_class", "american"),
    [
        ("NQ", True),  # the quarterly class: American (359A01.G.1)
        ("QN3", False),  # the third-Friday weekly, also listed on quarterly Fridays
        ("QNE", False),  # end of month
        ("Q3C", False),  # a daily
        ("nq", False),  # classes are matched exactly, as IBKR sends them
    ],
)
def test_american_goes_by_the_trading_class(
    instrument: InstrumentProfile, trading_class: str, american: bool
) -> None:
    assert is_american(quote(trading_class=trading_class), instrument) is american


@pytest.mark.parametrize("bad", ["NQ", None, date(2026, 12, 18)])
def test_is_american_takes_an_option_quote_only(instrument: InstrumentProfile, bad: object) -> None:
    with pytest.raises(TypeError, match="quote must be an OptionQuote"):
        is_american(bad, instrument)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "european"),
    [
        ("Q1A", True),  # Monday daily, week 1
        ("Q4D", True),  # Thursday daily, week 4 (seen 2026-10-22)
        ("Q5D", True),
        ("QN1", True),  # Friday weeklies
        ("QN3", True),
        ("QNE", True),  # end of month
        ("NQ", False),  # the quarterly: American
        ("ZZZ", False),  # never seen: refused, not assumed European
        ("Q6A", False),
        ("Q1E", False),
        ("QN3X", False),  # the whole name must match
        ("XQN3", False),
        ("q4d", False),
    ],
)
def test_only_listed_european_classes_are_european(
    instrument: InstrumentProfile, name: str, european: bool
) -> None:
    assert is_european_class(name, instrument) is european


def test_an_unknown_class_is_vetoed_not_valued(theta: ThetaAgent) -> None:
    check = theta.assess(plan(quote(trading_class="ZZZ")), FORWARD, NOW)
    assert not check.allowed
    assert "ZZZ" in check.reason
    assert "not a known European class" in check.reason
    assert check.expires_at is None
    assert check.implied_vol is None


def test_the_date_does_not_make_an_option_american(instrument: InstrumentProfile) -> None:
    # IBKR lists a weekly (QN3, on the next quarterly future) beside the quarterly on a
    # quarterly Friday (checked 2026-10-22: 2026-12-18 lists classes NQ and QN3).
    quarterly_friday = date(2026, 12, 18)
    assert not is_american(quote(expiry=quarterly_friday, trading_class="QN3"), instrument)
    assert is_american(quote(expiry=quarterly_friday, trading_class="NQ"), instrument)


def test_every_configured_american_class_is_american(instrument: InstrumentProfile) -> None:
    rules = replace(instrument.option_expiry, american_trading_classes=frozenset({"NQ", "XQ"}))
    two = replace(instrument, option_expiry=rules)
    assert is_american(quote(trading_class="XQ"), two)
    assert is_american(quote(trading_class="NQ"), two)
    assert not is_american(quote(trading_class="QN3"), two)


# --- a normal entry ---------------------------------------------------------------


def test_a_normal_entry_is_allowed_and_carries_its_numbers(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    q = quote()
    check = theta.assess(plan(q), FORWARD, NOW)
    years, vol, decay = expected(q, NOW, settings, instrument)
    assert isinstance(check, ThetaCheck)
    assert check.allowed, check.reason
    assert check.contract_id == q.contract_id
    assert check.contracts == 1
    assert check.expires_at == expiry_moment(WED, instrument)
    assert check.years == pytest.approx(years, rel=1e-12)
    assert check.implied_vol == pytest.approx(vol, rel=1e-12)
    assert check.decay_points == pytest.approx(decay, rel=1e-9)
    assert decay > 0
    assert check.decay_dollars == D(str(check.decay_points)) * instrument.point_value
    assert check.budget_dollars == risk.profit_target * settings.max_decay_pct_of_target / 100
    assert check.budget_dollars > check.decay_dollars


def test_decay_scales_with_the_contracts(theta: ThetaAgent, instrument: InstrumentProfile) -> None:
    q = quote()
    one = theta.assess(plan(q, 1), FORWARD, NOW)
    three = theta.assess(plan(q, 3), FORWARD, NOW)
    assert one.decay_dollars is not None
    assert three.decay_dollars is not None
    assert three.decay_dollars == one.decay_dollars * 3
    assert three.decay_points == one.decay_points  # per option, not per position
    assert (one.contracts, three.contracts) == (1, 3)


def test_a_put_is_valued_as_a_put(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    q = quote(strike=20025, right=Right.PUT)
    check = theta.assess(plan(q), FORWARD, NOW)
    _, vol, decay = expected(q, NOW, settings, instrument)
    assert check.allowed, check.reason
    assert check.implied_vol == pytest.approx(vol, rel=1e-12)
    assert check.decay_points == pytest.approx(decay, rel=1e-9)
    # A call at the same strike and mid implies a different volatility.
    call = theta.assess(plan(quote(strike=20025)), FORWARD, NOW)
    assert call.implied_vol != pytest.approx(check.implied_vol, rel=1e-6)


def test_the_underlying_price_given_is_the_forward(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    q = quote()
    check = theta.assess(plan(q), D(19950), NOW)
    _, vol, _ = expected(q, NOW, settings, instrument, forward=D(19950))
    assert check.implied_vol == pytest.approx(vol, rel=1e-12)


def test_the_configured_rate_is_used(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    q = quote()
    other = replace(settings, rate=settings.rate + D("0.05"))
    base = theta.assess(plan(q), FORWARD, NOW)
    moved = ThetaAgent(other, instrument, risk).assess(plan(q), FORWARD, NOW)
    _, vol, _ = expected(q, NOW, other, instrument)
    assert moved.implied_vol == pytest.approx(vol, rel=1e-12)
    assert moved.implied_vol != pytest.approx(base.implied_vol, rel=1e-9)


def test_time_is_counted_on_the_calendar_through_a_weekend(
    theta: ThetaAgent, instrument: InstrumentProfile
) -> None:
    friday = datetime(2026, 9, 11, 7, 0, tzinfo=PT).astimezone(UTC)
    monday = date(2026, 9, 14)
    check = theta.assess(plan(quote(expiry=monday)), FORWARD, friday)
    calendar = (expiry_moment(monday, instrument) - friday) / YEAR
    assert check.years == pytest.approx(calendar, rel=1e-12)  # weekend hours included


def test_the_hold_length_drives_the_decay(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    q = quote()
    longer = replace(settings, planned_hold_minutes=settings.planned_hold_minutes * 2)
    base = theta.assess(plan(q), FORWARD, NOW)
    long = ThetaAgent(longer, instrument, risk).assess(plan(q), FORWARD, NOW)
    _, _, decay = expected(q, NOW, longer, instrument)
    assert long.decay_points == pytest.approx(decay, rel=1e-9)
    assert base.decay_points is not None
    assert long.decay_points is not None
    assert long.decay_points > base.decay_points


# --- the decay budget -------------------------------------------------------------


def test_decay_over_the_budget_is_vetoed(theta: ThetaAgent, instrument: InstrumentProfile) -> None:
    # Four hours before expiry the same hold costs far more of the premium.
    late = datetime(2026, 9, 16, 9, 0, tzinfo=PT).astimezone(UTC)
    q = quote(bid=50, ask=52)
    one = theta.assess(plan(q, 1), FORWARD, late)
    assert one.decay_dollars is not None
    contracts = int(one.budget_dollars // one.decay_dollars) + 1
    check = theta.assess(plan(q, contracts), FORWARD, late)
    assert not check.allowed
    assert check.decay_dollars is not None
    assert check.decay_dollars > check.budget_dollars
    assert "decay" in check.reason
    assert str(check.budget_dollars) in check.reason


def test_decay_exactly_at_the_budget_is_allowed(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile, risk: RiskProfile
) -> None:
    q = quote()
    decay = theta.assess(plan(q), FORWARD, NOW).decay_dollars
    assert decay is not None
    exact = replace(settings, max_decay_pct_of_target=decay * 100 / risk.profit_target)
    at = ThetaAgent(exact, instrument, risk).assess(plan(q), FORWARD, NOW)
    assert at.budget_dollars == decay
    assert at.allowed, at.reason
    under = replace(settings, max_decay_pct_of_target=exact.max_decay_pct_of_target * D("0.999"))
    assert not ThetaAgent(under, instrument, risk).assess(plan(q), FORWARD, NOW).allowed


# --- hard dangers -----------------------------------------------------------------


def test_an_option_expiring_within_the_hold_is_vetoed(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    hold = timedelta(minutes=settings.planned_hold_minutes)
    q = quote(bid=30, ask=31)  # a price an option an hour from expiry can have
    exactly = theta.assess(plan(q), FORWARD, moment - hold)
    assert not exactly.allowed
    assert "expires" in exactly.reason
    assert exactly.years is None
    assert exactly.implied_vol is None
    assert exactly.expires_at == moment
    assert exactly.decay_dollars is None
    # One second more and it is valued (its decay over the hold is then nearly the
    # whole premium, which the budget judges separately).
    just_more = theta.assess(plan(q), FORWARD, moment - hold - timedelta(seconds=1))
    assert "expires" not in just_more.reason
    assert just_more.implied_vol is not None
    assert just_more.decay_points is not None
    assert just_more.decay_points == pytest.approx(
        expected(q, moment - hold - timedelta(seconds=1), settings, instrument)[2], rel=1e-9
    )


def test_the_early_close_shortens_the_time_left(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    day = date(2026, 11, 27)  # a stock market half day
    hold = timedelta(minutes=settings.planned_hold_minutes)
    early = instrument.option_expiry.early_close_time
    as_of = datetime.combine(day, early, PT).astimezone(UTC) - hold
    check = theta.assess(plan(quote(expiry=day)), FORWARD, as_of)
    assert not check.allowed  # it would pass against the normal 13:00 expiry
    assert "expires" in check.reason


def test_an_expired_option_is_vetoed(theta: ThetaAgent, instrument: InstrumentProfile) -> None:
    after = expiry_moment(WED, instrument) + timedelta(minutes=1)
    check = theta.assess(plan(quote()), FORWARD, after)
    assert not check.allowed
    assert check.years is None


def test_a_quarterly_american_option_is_vetoed(theta: ThetaAgent) -> None:
    check = theta.assess(plan(quote(trading_class="NQ")), FORWARD, NOW)
    assert not check.allowed
    assert "American" in check.reason
    assert "NQ" in check.reason
    assert check.expires_at is None
    assert check.implied_vol is None


def test_a_weekly_on_a_quarterly_friday_is_valued_not_vetoed(theta: ThetaAgent) -> None:
    friday = date(2026, 9, 18)  # third Friday of September
    thursday = datetime(2026, 9, 17, 7, 0, tzinfo=PT).astimezone(UTC)
    q = quote(expiry=friday, trading_class="QN3")
    check = theta.assess(plan(q), FORWARD, thursday)
    assert "American" not in check.reason
    assert check.implied_vol is not None


def test_a_quote_no_volatility_can_produce_is_vetoed(theta: ThetaAgent) -> None:
    # A deep in-the-money call quoted under its intrinsic value.
    q = quote(strike=19000, bid=900, ask=905)
    check = theta.assess(plan(q), FORWARD, NOW)
    assert not check.allowed
    assert "implied volatility" in check.reason
    assert check.implied_vol is None


def test_implied_volatility_below_the_minimum_is_vetoed(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    years = (expiry_moment(WED, instrument) - NOW) / YEAR
    args = {"forward": float(FORWARD), "strike": 20000.0, "years": years}
    cheap = price(Right76.CALL, vol=float(settings.min_implied_vol) * 0.8, rate=0.0, **args)
    q = quote(bid=round(cheap * 4) / 4 - 0.25, ask=round(cheap * 4) / 4)
    check = theta.assess(plan(q), FORWARD, NOW)
    assert not check.allowed
    assert str(settings.min_implied_vol) in check.reason


def test_implied_volatility_above_the_maximum_is_vetoed(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    years = (expiry_moment(WED, instrument) - NOW) / YEAR
    args = {"forward": float(FORWARD), "strike": 20000.0, "years": years}
    rich = price(Right76.CALL, vol=float(settings.max_implied_vol) * 1.2, rate=0.0, **args)
    q = quote(bid=round(rich), ask=round(rich) + 2)
    check = theta.assess(plan(q), FORWARD, NOW)
    assert not check.allowed
    assert str(settings.max_implied_vol) in check.reason


def test_a_negative_modelled_decay_is_kept_and_allowed(
    theta: ThetaAgent, settings: ThetaSettings, instrument: InstrumentProfile
) -> None:
    # A deep in-the-money European put with a positive rate GAINS value as time passes
    # (its value is mostly the discounted intrinsic value): the decay is negative.
    years = (expiry_moment(WED, instrument) - NOW) / YEAR
    worth = price(
        Right76.PUT,
        forward=float(FORWARD),
        strike=22000.0,  # about six standard deviations in the money: no time value left
        years=years,
        vol=0.3,
        rate=float(settings.rate),
    )
    q = quote(strike=22000, right=Right.PUT, bid=worth, ask=worth)
    check = theta.assess(plan(q), FORWARD, NOW)
    _, _, decay = expected(q, NOW, settings, instrument)
    assert decay < 0
    assert check.decay_points == pytest.approx(decay, rel=1e-6)
    assert check.decay_dollars is not None
    assert check.decay_dollars < 0
    assert check.allowed, check.reason


def test_inputs_the_model_cannot_take_are_vetoed_not_raised(theta: ThetaAgent) -> None:
    check = theta.assess(plan(quote()), D("1e400"), NOW)  # beyond float range
    assert not check.allowed
    assert "cannot value" in check.reason
    assert check.implied_vol is None


@pytest.mark.parametrize("bad", [datetime(2026, 9, 16, 13, 0, tzinfo=PT), "2026-09-16"])
def test_the_expiry_moment_takes_a_date_only(instrument: InstrumentProfile, bad: object) -> None:
    with pytest.raises(TypeError, match="expiry must be a date"):
        expiry_moment(bad, instrument)  # type: ignore[arg-type]


def test_the_time_must_be_utc(theta: ThetaAgent) -> None:
    with pytest.raises(ValueError, match="as_of"):
        theta.assess(plan(quote()), FORWARD, NOW.astimezone(PT))


def test_the_agent_id(theta: ThetaAgent) -> None:
    assert theta.agent_id == "theta"
