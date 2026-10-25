"""The Greeks vetoes (decision GV-1): Vega, Delta and Gamma, veto only, tested from the rule.

Each agent values the chosen option with Black-76 at the implied volatility of its
quote's mid (solved inside the vega band, at theta's rate, calendar time to the option's
expiry moment, the chain's underlying price as the forward) and blocks the entry only on
its own hard danger:

- Vega: no volatility inside [vega.min_implied_vol, vega.max_implied_vol] prices the mid;
- Delta: the Black-76 delta (negative for a put) has an absolute value outside
  [delta.min_abs_delta, delta.max_abs_delta];
- Gamma: less than gamma.min_hours_to_expiry is left to the expiry moment.

Anything an agent cannot value (an American or unknown class, an expired option, inputs
the model cannot take, a volatility outside the band) means no entry: fail closed.
Expected values are computed here from the certified Black-76 and implied-volatility
modules, never copied from the code under test."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.contract_picker import ContractChoice
from footprint.agents.greeks import DeltaAgent, GammaAgent, GreekCheck, VegaAgent
from footprint.core.black76 import Right76, greeks, price
from footprint.core.config import (
    DeltaSettings,
    GammaSettings,
    InstrumentProfile,
    StrategyProfile,
    VegaSettings,
)
from footprint.core.expiry import expiry_moment
from footprint.core.implied_vol import implied_vol
from footprint.core.options import OptionQuote, Right
from footprint.decision.sizing import EntryPlan

PT = ZoneInfo("America/Los_Angeles")
D = Decimal
YEAR = timedelta(days=365)
HOUR = timedelta(hours=1)
TUE = date(2026, 9, 15)
WED = date(2026, 9, 16)
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=PT).astimezone(UTC)
FORWARD = D(20010)

Agent = VegaAgent | DeltaAgent | GammaAgent


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


def priced(
    vol: float,
    instrument: InstrumentProfile,
    *,
    strike: float = 20000,
    right: Right = Right.CALL,
    expiry: date = WED,
    as_of: datetime = NOW,
    rate: float = 0.0,
) -> OptionQuote:
    """A quote whose mid is the Black-76 value at `vol` (bid = ask = that value)."""
    years = (expiry_moment(expiry, instrument) - as_of) / YEAR
    r76 = Right76.CALL if right is Right.CALL else Right76.PUT
    worth = price(r76, forward=float(FORWARD), strike=strike, years=years, vol=vol, rate=rate)
    return quote(expiry=expiry, strike=strike, right=right, bid=worth, ask=worth)


@pytest.fixture
def band(strategy: StrategyProfile) -> VegaSettings:
    return strategy.vega


@pytest.fixture
def rate(strategy: StrategyProfile) -> Decimal:
    return strategy.theta.rate


@pytest.fixture
def vega(band: VegaSettings, rate: Decimal, instrument: InstrumentProfile) -> VegaAgent:
    return VegaAgent(band, rate, instrument)


@pytest.fixture
def delta(
    strategy: StrategyProfile, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> DeltaAgent:
    return DeltaAgent(strategy.delta, band, rate, instrument)


@pytest.fixture
def gamma(
    strategy: StrategyProfile, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> GammaAgent:
    return GammaAgent(strategy.gamma, band, rate, instrument)


@pytest.fixture(params=["vega", "delta", "gamma"])
def agent(request: pytest.FixtureRequest) -> Agent:
    found: Agent = request.getfixturevalue(request.param)
    return found


def expected(
    q: OptionQuote,
    band: VegaSettings,
    rate: Decimal,
    instrument: InstrumentProfile,
    as_of: datetime = NOW,
    forward: Decimal = FORWARD,
) -> tuple[datetime, float, float, float, float, float]:
    """(expiry moment, years, implied vol, delta, gamma, vega), worked out independently."""
    moment = expiry_moment(q.expiry, instrument)
    years = (moment - as_of) / YEAR
    right = Right76.CALL if q.right is Right.CALL else Right76.PUT
    vol = implied_vol(
        right,
        forward=float(forward),
        strike=float(q.strike),
        years=years,
        rate=float(rate),
        price=float(q.mid),
        low=float(band.min_implied_vol),
        high=float(band.max_implied_vol),
    )
    model = greeks(
        right,
        forward=float(forward),
        strike=float(q.strike),
        years=years,
        vol=vol,
        rate=float(rate),
    )
    return moment, years, vol, model.delta, model.gamma, model.vega


# --- shared: what every Greeks agent values, and what it refuses -------------------


def test_each_agent_has_its_id(vega: VegaAgent, delta: DeltaAgent, gamma: GammaAgent) -> None:
    assert (vega.agent_id, delta.agent_id, gamma.agent_id) == ("vega", "delta", "gamma")


def test_a_normal_entry_is_allowed_by_every_agent_and_carries_the_numbers(
    agent: Agent, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    q = quote()
    check = agent.assess(plan(q, contracts=1), FORWARD, NOW)
    moment, years, vol, d, g, v = expected(q, band, rate, instrument)
    assert isinstance(check, GreekCheck)
    assert check.allowed, check.reason
    assert check.agent_id == agent.agent_id
    assert (check.contract_id, check.contracts) == (q.contract_id, 1)
    assert check.expires_at == moment
    assert check.years == pytest.approx(years, rel=1e-12)
    assert check.implied_vol == pytest.approx(vol, rel=1e-12)
    assert check.delta == pytest.approx(d, rel=1e-12)
    assert check.gamma == pytest.approx(g, rel=1e-12)
    assert check.vega == pytest.approx(v, rel=1e-12)


def test_the_check_names_the_sized_contracts(agent: Agent) -> None:
    check = agent.assess(plan(quote(), contracts=3), FORWARD, NOW)
    assert check.contracts == 3


def test_the_underlying_price_given_is_the_forward(
    agent: Agent, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    q = quote()
    other = D(20060)
    check = agent.assess(plan(q), other, NOW)
    _, _, vol, d, _, _ = expected(q, band, rate, instrument, forward=other)
    assert check.implied_vol == pytest.approx(vol, rel=1e-12)
    assert check.delta == pytest.approx(d, rel=1e-12)


def test_the_configured_rate_is_used(
    strategy: StrategyProfile, band: VegaSettings, instrument: InstrumentProfile
) -> None:
    q = quote(strike=19900)  # in the money: the rate moves the value visibly
    other = D("0.2")
    agents: tuple[Agent, ...] = (
        VegaAgent(band, other, instrument),
        DeltaAgent(strategy.delta, band, other, instrument),
        GammaAgent(strategy.gamma, band, other, instrument),
    )
    _, _, vol, d, _, _ = expected(q, band, other, instrument)
    _, _, base_vol, _, _, _ = expected(q, band, strategy.theta.rate, instrument)
    assert vol != pytest.approx(base_vol, rel=1e-9)
    for a in agents:
        check = a.assess(plan(q), FORWARD, NOW)
        assert check.implied_vol == pytest.approx(vol, rel=1e-12), a.agent_id
        assert check.delta == pytest.approx(d, rel=1e-12), a.agent_id


def test_time_is_counted_on_the_calendar_through_a_weekend(
    agent: Agent, instrument: InstrumentProfile
) -> None:
    friday = datetime(2026, 9, 18, 7, 0, tzinfo=PT).astimezone(UTC)
    q = quote(expiry=date(2026, 9, 21))  # Monday
    check = agent.assess(plan(q), FORWARD, friday)
    assert check.years == pytest.approx(
        (expiry_moment(q.expiry, instrument) - friday) / YEAR, rel=1e-12
    )


@pytest.mark.parametrize("trading_class", ["NQ", "ZZZ"])
def test_an_american_or_unknown_class_is_refused_not_valued(
    agent: Agent, trading_class: str
) -> None:
    check = agent.assess(plan(quote(trading_class=trading_class)), FORWARD, NOW)
    assert not check.allowed
    assert trading_class in check.reason
    assert check.implied_vol is None
    assert check.delta is None


def test_an_american_class_is_named_as_such(agent: Agent) -> None:
    check = agent.assess(plan(quote(trading_class="NQ")), FORWARD, NOW)
    assert "American" in check.reason


def test_an_unknown_class_is_named_as_such(agent: Agent) -> None:
    check = agent.assess(plan(quote(trading_class="ZZZ")), FORWARD, NOW)
    assert "not a known European class" in check.reason


def test_an_expired_option_is_refused(agent: Agent, instrument: InstrumentProfile) -> None:
    after = expiry_moment(TUE, instrument) + timedelta(seconds=1)
    check = agent.assess(plan(quote(expiry=TUE)), FORWARD, after)
    assert not check.allowed
    assert "expired" in check.reason
    assert check.implied_vol is None


def test_an_option_at_its_expiry_moment_is_refused(
    agent: Agent, instrument: InstrumentProfile
) -> None:
    check = agent.assess(plan(quote(expiry=TUE)), FORWARD, expiry_moment(TUE, instrument))
    assert not check.allowed
    assert "expired" in check.reason  # refused as expired, not left to the solver
    assert check.implied_vol is None


def test_inputs_the_model_cannot_take_are_refused_not_raised(agent: Agent) -> None:
    check = agent.assess(plan(quote()), D("1e400"), NOW)  # beyond float range
    assert not check.allowed
    assert "cannot value" in check.reason
    assert check.implied_vol is None


def test_the_time_must_be_utc(agent: Agent) -> None:
    with pytest.raises(ValueError, match="as_of"):
        agent.assess(plan(quote()), FORWARD, NOW.astimezone(PT))


# --- Vega: implied volatility outside the band ------------------------------------


def test_implied_volatility_below_the_band_is_vetoed(
    vega: VegaAgent, band: VegaSettings, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    # Inside theta's bounds (Theta would let it through) but under the vega band.
    low = float(strategy.theta.min_implied_vol + band.min_implied_vol) / 2
    check = vega.assess(plan(priced(low, instrument)), FORWARD, NOW)
    assert not check.allowed
    assert "below" in check.reason
    assert str(band.min_implied_vol) in check.reason
    assert check.implied_vol is None


def test_implied_volatility_above_the_band_is_vetoed(
    vega: VegaAgent, band: VegaSettings, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    high = float(strategy.theta.max_implied_vol + band.max_implied_vol) / 2
    check = vega.assess(plan(priced(high, instrument)), FORWARD, NOW)
    assert not check.allowed
    assert "above" in check.reason
    assert str(band.max_implied_vol) in check.reason


def test_a_quote_under_its_intrinsic_value_is_vetoed(vega: VegaAgent) -> None:
    q = quote(strike=19000, bid=900, ask=905)  # intrinsic 1010
    check = vega.assess(plan(q), FORWARD, NOW)
    assert not check.allowed
    assert "below" in check.reason


def test_the_band_edges_are_inside(
    rate: Decimal, band: VegaSettings, instrument: InstrumentProfile
) -> None:
    q = quote()
    _, _, vol, _, _, _ = expected(q, band, rate, instrument)
    at_low = VegaAgent(
        replace(band, min_implied_vol=D(repr(vol)) * D("0.999999")), rate, instrument
    )
    assert at_low.assess(plan(q), FORWARD, NOW).allowed
    over_low = VegaAgent(
        replace(band, min_implied_vol=D(repr(vol)) * D("1.000001")), rate, instrument
    )
    assert not over_low.assess(plan(q), FORWARD, NOW).allowed
    at_high = VegaAgent(
        replace(band, max_implied_vol=D(repr(vol)) * D("1.000001")), rate, instrument
    )
    assert at_high.assess(plan(q), FORWARD, NOW).allowed
    under_high = VegaAgent(
        replace(band, max_implied_vol=D(repr(vol)) * D("0.999999")), rate, instrument
    )
    assert not under_high.assess(plan(q), FORWARD, NOW).allowed


def test_vega_reasons_carry_the_numbers(vega: VegaAgent, band: VegaSettings) -> None:
    check = vega.assess(plan(quote()), FORWARD, NOW)
    assert check.allowed
    assert check.implied_vol is not None
    assert f"{check.implied_vol:.4f}" in check.reason
    assert str(band.min_implied_vol) in check.reason
    assert str(band.max_implied_vol) in check.reason


# --- Delta: |delta| outside the band ----------------------------------------------


def test_an_at_the_money_put_has_a_negative_delta_and_is_allowed(
    delta: DeltaAgent, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    q = quote(right=Right.PUT, strike=20025)
    check = delta.assess(plan(q), FORWARD, NOW)
    _, _, _, d, _, _ = expected(q, band, rate, instrument)
    assert d < 0
    assert check.delta == pytest.approx(d, rel=1e-12)
    assert check.allowed, check.reason


@pytest.mark.parametrize(
    ("strike", "right"),
    [
        (20400, Right.CALL),  # far out of the money: |delta| under the band
        (19600, Right.CALL),  # deep in the money: |delta| over the band
        (19600, Right.PUT),  # far out of the money put
        (20400, Right.PUT),  # deep in the money put
    ],
)
def test_a_delta_outside_the_band_is_vetoed(
    delta: DeltaAgent,
    strategy: StrategyProfile,
    instrument: InstrumentProfile,
    strike: float,
    right: Right,
) -> None:
    q = priced(0.3, instrument, strike=strike, right=right, rate=float(strategy.theta.rate))
    check = delta.assess(plan(q), FORWARD, NOW)
    assert check.delta is not None
    magnitude = abs(check.delta)
    s = strategy.delta
    assert not float(s.min_abs_delta) <= magnitude <= float(s.max_abs_delta)
    assert not check.allowed
    assert f"{check.delta:.4f}" in check.reason
    assert str(s.min_abs_delta) in check.reason
    assert str(s.max_abs_delta) in check.reason


def test_a_put_is_judged_by_the_size_of_its_delta(
    strategy: StrategyProfile, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    # Mirrors a call inside the band: a signed comparison would refuse every put.
    q = quote(right=Right.PUT, strike=20025)
    _, _, _, d, _, _ = expected(q, band, rate, instrument)
    agent = DeltaAgent(strategy.delta, band, rate, instrument)
    assert float(strategy.delta.min_abs_delta) < -d < float(strategy.delta.max_abs_delta)
    assert agent.assess(plan(q), FORWARD, NOW).allowed


@pytest.mark.parametrize("right", [Right.CALL, Right.PUT])
def test_the_delta_band_edges_are_inside(
    strategy: StrategyProfile,
    band: VegaSettings,
    rate: Decimal,
    instrument: InstrumentProfile,
    right: Right,
) -> None:
    q = quote(right=right)
    _, _, _, d, _, _ = expected(q, band, rate, instrument)
    size = D(repr(abs(d)))
    s = strategy.delta

    def judged(low: Decimal, high: Decimal) -> bool:
        settings = DeltaSettings(min_abs_delta=low, max_abs_delta=high)
        return DeltaAgent(settings, band, rate, instrument).assess(plan(q), FORWARD, NOW).allowed

    assert judged(size, s.max_abs_delta)  # exactly at the minimum
    assert judged(s.min_abs_delta, size)  # exactly at the maximum
    nudge = D("1e-9")
    assert not judged(size + nudge, s.max_abs_delta)
    assert not judged(s.min_abs_delta, size - nudge)


def test_delta_refuses_a_volatility_outside_the_vega_band(
    delta: DeltaAgent, band: VegaSettings, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    high = float(strategy.theta.max_implied_vol + band.max_implied_vol) / 2
    check = delta.assess(plan(priced(high, instrument)), FORWARD, NOW)
    assert not check.allowed
    assert "cannot value" in check.reason
    assert check.delta is None


# --- Gamma: too close to expiry -----------------------------------------------------


def test_an_option_inside_the_minimum_hours_is_vetoed(
    gamma: GammaAgent, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    minimum = timedelta(hours=float(strategy.gamma.min_hours_to_expiry))
    as_of = moment - minimum + timedelta(minutes=30)
    check = gamma.assess(plan(quote()), FORWARD, as_of)
    assert not check.allowed
    assert check.expires_at == moment
    assert check.years == pytest.approx((moment - as_of) / YEAR, rel=1e-12)
    left = (moment - as_of) / HOUR
    assert f"{left:.2f}" in check.reason
    assert str(strategy.gamma.min_hours_to_expiry) in check.reason
    assert "expiry" in check.reason


def test_exactly_the_minimum_hours_is_allowed(
    gamma: GammaAgent, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    minimum = timedelta(hours=float(strategy.gamma.min_hours_to_expiry))
    assert gamma.assess(plan(quote()), FORWARD, moment - minimum).allowed
    one_second_less = moment - minimum + timedelta(seconds=1)
    assert not gamma.assess(plan(quote()), FORWARD, one_second_less).allowed


def test_the_minimum_hours_come_from_config(
    strategy: StrategyProfile, band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    as_of = moment - timedelta(hours=10)
    tight = GammaAgent(GammaSettings(min_hours_to_expiry=D(11)), band, rate, instrument)
    loose = GammaAgent(GammaSettings(min_hours_to_expiry=D(9)), band, rate, instrument)
    assert not tight.assess(plan(quote()), FORWARD, as_of).allowed
    assert loose.assess(plan(quote()), FORWARD, as_of).allowed


def test_a_half_hour_minimum_is_honoured(
    band: VegaSettings, rate: Decimal, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    agent = GammaAgent(GammaSettings(min_hours_to_expiry=D("0.5")), band, rate, instrument)
    at = moment - timedelta(minutes=30)
    q = priced(0.3, instrument, as_of=at, rate=float(rate))  # a believable quote that late
    assert agent.assess(plan(q), FORWARD, at).allowed
    assert not agent.assess(plan(q), FORWARD, at + timedelta(minutes=1)).allowed


def test_the_early_close_puts_an_option_inside_the_danger_zone(
    gamma: GammaAgent, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    half_day = date(2026, 11, 27)  # the stock market's half day: expiry at noon CT
    normal = date(2026, 11, 20)
    hours = float(strategy.gamma.min_hours_to_expiry)
    for day in (half_day, normal):
        normal_moment = expiry_moment(normal, instrument)
        as_of = datetime.combine(day, normal_moment.astimezone(PT).time(), PT).astimezone(UTC)
        as_of -= timedelta(hours=hours) + timedelta(minutes=30)
        check = gamma.assess(plan(quote(expiry=day)), FORWARD, as_of)
        assert check.allowed is (day == normal), (day, check.reason)


def test_gamma_vetoes_on_time_even_when_it_cannot_value(
    gamma: GammaAgent, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    moment = expiry_moment(WED, instrument)
    as_of = moment - timedelta(hours=float(strategy.gamma.min_hours_to_expiry)) / 2
    check = gamma.assess(plan(quote(strike=19000, bid=900, ask=905)), FORWARD, as_of)
    assert not check.allowed
    assert str(strategy.gamma.min_hours_to_expiry) in check.reason
    assert check.expires_at == moment


def test_gamma_refuses_what_it_cannot_value_with_time_left(gamma: GammaAgent) -> None:
    check = gamma.assess(plan(quote(strike=19000, bid=900, ask=905)), FORWARD, NOW)
    assert not check.allowed
    assert "cannot value" in check.reason


def test_an_allowed_gamma_reason_carries_the_hours(
    gamma: GammaAgent, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    check = gamma.assess(plan(quote()), FORWARD, NOW)
    assert check.allowed
    left = (expiry_moment(WED, instrument) - NOW) / HOUR
    assert f"{left:.2f}" in check.reason
    assert check.gamma is not None
    assert f"{check.gamma:.6f}" in check.reason


@pytest.mark.parametrize("side", ["below", "above"])
def test_a_vega_veto_is_named_a_hard_danger_with_its_timing(
    vega: VegaAgent,
    band: VegaSettings,
    strategy: StrategyProfile,
    instrument: InstrumentProfile,
    side: str,
) -> None:
    # Vega's own danger is worded as one, not as an option it could not value, and it
    # keeps the expiry moment and the time left for the journal.
    theta = strategy.theta
    vol = (
        float(theta.min_implied_vol + band.min_implied_vol) / 2
        if side == "below"
        else float(theta.max_implied_vol + band.max_implied_vol) / 2
    )
    check = vega.assess(plan(priced(vol, instrument)), FORWARD, NOW)
    assert not check.allowed
    assert "hard danger" in check.reason
    assert "cannot value" not in check.reason
    moment = expiry_moment(WED, instrument)
    assert check.expires_at == moment
    assert check.years == pytest.approx((moment - NOW) / YEAR, rel=1e-12)
