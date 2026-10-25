"""Theta's decay exit (decision DX-1), tested from the rule:

- only a trade in profit (open profit on the bid, net of round-trip fees, above 0) and
  below risk.profit_target, with the trail not locked, is ever judged; a losing trade
  is left to its stop;
- the option is valued exactly as Theta values an entry (B76-1: Black-76 at the implied
  volatility of the quote's mid, calendar time to the expiry moment), over the next
  `decay_exit.window_minutes`, for the held contracts;
- that decay above `decay_exit.max_pct_of_open_profit` percent of the open profit says
  "out"; at or below it, stay;
- anything that cannot be valued (no chain, a stale or future chain, the contract not
  in it, no volatility in range, an American or unknown class, an option expiring
  within the window) says "stay": never sell on a guess.

Expected values are computed here from the certified Black-76 module, never copied from
the code under test."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.agents.theta import DecayExit, DecayExitCheck
from footprint.core.black76 import Right76, price
from footprint.core.config import (
    ExecutionProfile,
    InstrumentProfile,
    RiskProfile,
    StrategyProfile,
    ThetaSettings,
)
from footprint.core.expiry import expiry_moment
from footprint.core.implied_vol import implied_vol
from footprint.core.options import OptionChain, OptionQuote, Right
from tests.agents.test_theta import FORWARD, NOW, PT, WED, YEAR, quote

D = Decimal
SEC = timedelta(seconds=1)


def switched_on(theta: ThetaSettings) -> ThetaSettings:
    """Theta's settings with the decay exit turned ON. It ships off until Andre rules
    on DX-2, so every test of the rule itself turns it on explicitly."""
    return replace(theta, decay_exit=replace(theta.decay_exit, enabled=True))


@pytest.fixture
def settings(strategy: StrategyProfile) -> ThetaSettings:
    return switched_on(strategy.theta)


class Chains:
    """A chain source that records every time it is asked."""

    def __init__(self, chain: OptionChain | None) -> None:
        self.chain = chain
        self.asked: list[datetime] = []

    def __call__(self, now: datetime) -> OptionChain | None:
        self.asked.append(now)
        return self.chain


def chain_of(*quotes: OptionQuote, ts: datetime = NOW, forward: Decimal = FORWARD) -> OptionChain:
    return OptionChain("NQ", ts, forward, tuple(quotes))


def exit_for(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
    chains: Callable[[datetime], OptionChain | None],
) -> DecayExit:
    return DecayExit(settings, instrument, risk, execution.quotes, chains)


@pytest.fixture
def make(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> Callable[[OptionChain | None], tuple[DecayExit, Chains]]:
    def build(chain: OptionChain | None) -> tuple[DecayExit, Chains]:
        chains = Chains(chain)
        return exit_for(settings, instrument, risk, execution, chains), chains

    return build


def expected(
    q: OptionQuote,
    as_of: datetime,
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    *,
    minutes: int | None = None,
    forward: Decimal = FORWARD,
) -> tuple[float, float]:
    """(implied vol, decay points over `minutes`, default the decay-exit window),
    worked out independently of the agent."""
    window = settings.decay_exit.window_minutes if minutes is None else minutes
    years = (expiry_moment(q.expiry, instrument) - as_of) / YEAR
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
    step = timedelta(minutes=window) / YEAR
    return vol, price(right, years=years, **args) - price(right, years=years - step, **args)


def dollars(points: float, instrument: InstrumentProfile, contracts: int = 1) -> Decimal:
    return D(str(points)) * instrument.point_value * contracts


def check(
    exit_: DecayExit,
    q: OptionQuote,
    open_profit: Decimal,
    *,
    contracts: int = 1,
    trailing: bool = False,
    now: datetime = NOW,
) -> DecayExitCheck:
    return exit_.check(q.contract_id, contracts, open_profit, trailing, now)


# --- the rule ---------------------------------------------------------------------


def test_decay_above_the_share_of_open_profit_says_out(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote()
    exit_, chains = make(chain_of(q))
    vol, points = expected(q, NOW, settings, instrument)
    decay = dollars(points, instrument)
    assert decay > 0
    verdict = check(exit_, q, decay)  # the decay is the whole open profit
    assert verdict.fire, verdict.reason
    assert verdict.decay_dollars == decay
    assert verdict.decay_points == pytest.approx(points, rel=1e-12)
    assert verdict.implied_vol == pytest.approx(vol, rel=1e-12)
    assert verdict.open_profit == decay
    assert not verdict.unvalued
    assert verdict.limit_dollars == decay * settings.decay_exit.max_pct_of_open_profit / 100
    assert verdict.underlying_price == FORWARD
    assert chains.asked == [NOW]
    # The journal line carries the numbers behind it.
    for text in (
        f"{decay:.2f}",
        f"{verdict.limit_dollars:.2f}",
        f"{decay:.2f}",
        str(settings.decay_exit.window_minutes),
        str(settings.decay_exit.max_pct_of_open_profit),
        q.contract_id,
        str(FORWARD),
        str(q.mid),
    ):
        assert text in verdict.reason, (text, verdict.reason)
    assert "decay exit" in verdict.reason


def test_decay_exactly_at_the_limit_stays_and_just_above_goes(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote()
    exit_, _ = make(chain_of(q))
    decay = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    at_limit = decay * 100 / settings.decay_exit.max_pct_of_open_profit
    at = check(exit_, q, at_limit)
    assert at.limit_dollars == decay
    assert not at.fire, at.reason
    assert at.decay_dollars == decay  # valued, and judged within the limit
    a_cent_less = check(exit_, q, at_limit - D("0.01"))
    assert a_cent_less.fire, a_cent_less.reason


def test_more_open_profit_than_the_decay_needs_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote()
    exit_, _ = make(chain_of(q))
    decay = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    verdict = check(exit_, q, decay * 10)
    assert not verdict.fire
    assert verdict.decay_dollars == decay
    assert "within" in verdict.reason
    assert not verdict.unvalued  # valued, and judged: a stay by the rule


@pytest.mark.parametrize("open_profit", [D(0), D("-0.01"), D(-500)])
def test_a_trade_not_in_profit_is_left_to_its_stop(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]], open_profit: Decimal
) -> None:
    q = quote()
    exit_, chains = make(chain_of(q))
    verdict = check(exit_, q, open_profit)
    assert not verdict.fire
    assert "not in profit" in verdict.reason
    assert verdict.decay_dollars is None
    assert not verdict.unvalued  # not judged: nothing to value
    assert chains.asked == []  # nothing is valued for it


def test_at_or_above_the_target_the_trail_takes_over(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]], risk: RiskProfile
) -> None:
    q = quote()
    exit_, chains = make(chain_of(q))
    for open_profit in (risk.profit_target, risk.profit_target + D("0.01")):
        verdict = check(exit_, q, open_profit)
        assert not verdict.fire
        assert "target" in verdict.reason
        assert not verdict.unvalued
    assert chains.asked == []


def test_just_below_the_target_a_big_decay_still_says_out(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
) -> None:
    late = datetime(2026, 9, 16, 9, 0, tzinfo=PT).astimezone(UTC)  # four hours left
    q = quote(bid=50, ask=52)
    exit_, _ = make(chain_of(q, ts=late))
    open_profit = risk.profit_target - D("0.01")
    one = dollars(expected(q, late, settings, instrument)[1], instrument)
    limit = open_profit * settings.decay_exit.max_pct_of_open_profit / 100
    contracts = int(limit // one) + 1
    verdict = check(exit_, q, open_profit, contracts=contracts, now=late)
    assert verdict.fire, verdict.reason
    assert verdict.decay_dollars == one * contracts


def test_a_trade_whose_trail_has_locked_is_left_to_the_trail(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    # Back below the target after the trail locked: the locked stop manages it.
    q = quote()
    exit_, chains = make(chain_of(q))
    decay = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    verdict = check(exit_, q, decay, trailing=True)
    assert not verdict.fire
    assert "trail" in verdict.reason
    assert not verdict.unvalued
    assert chains.asked == []


def test_decay_scales_with_the_contracts_held(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote()
    exit_, _ = make(chain_of(q))
    one = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    three = check(exit_, q, one * 10, contracts=3)
    assert three.decay_dollars == one * 3
    assert three.decay_points is not None
    assert three.decay_points == pytest.approx(expected(q, NOW, settings, instrument)[1])
    # 3x the decay against the same open profit: what stayed for one goes for three.
    assert not check(exit_, q, one * 3, contracts=1).fire
    assert check(exit_, q, one * 3, contracts=3).decay_dollars == one * 3
    assert check(exit_, q, one * 5, contracts=3).fire


def test_the_window_is_the_decay_exit_window_not_the_planned_hold(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    assert settings.decay_exit.window_minutes != settings.planned_hold_minutes
    q = quote()
    exit_, _ = make(chain_of(q))
    verdict = check(exit_, q, D(500))
    window = expected(q, NOW, settings, instrument)[1]
    hold = expected(q, NOW, settings, instrument, minutes=settings.planned_hold_minutes)[1]
    assert verdict.decay_points == pytest.approx(window, rel=1e-12)
    assert verdict.decay_points != pytest.approx(hold, rel=1e-3)


def test_the_window_comes_from_config(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> None:
    q = quote()
    longer = replace(settings, decay_exit=replace(settings.decay_exit, window_minutes=90))
    exit_ = exit_for(longer, instrument, risk, execution, Chains(chain_of(q)))
    verdict = check(exit_, q, D(500))
    assert verdict.decay_points == pytest.approx(
        expected(q, NOW, settings, instrument, minutes=90)[1], rel=1e-12
    )
    assert "90" in verdict.reason


def test_the_share_comes_from_config(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> None:
    q = quote()
    decay = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    open_profit = decay * 4  # the decay is 25% of it
    stays = exit_for(settings, instrument, risk, execution, Chains(chain_of(q)))
    assert not check(stays, q, open_profit).fire  # 25% is within the drafted 50%
    share = replace(settings.decay_exit, max_pct_of_open_profit=D(20))
    tighter = replace(settings, decay_exit=share)
    goes = exit_for(tighter, instrument, risk, execution, Chains(chain_of(q)))
    verdict = check(goes, q, open_profit)
    assert verdict.fire
    assert verdict.limit_dollars == open_profit * 20 / 100


def test_the_underlying_and_the_mid_come_from_the_chain(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote(bid=180, ask=184)
    forward = FORWARD + 40
    exit_, _ = make(chain_of(quote(strike=20100), q, ts=NOW, forward=forward))
    vol, points = expected(q, NOW, settings, instrument, forward=forward)
    verdict = check(exit_, q, D(500))
    assert verdict.implied_vol == pytest.approx(vol, rel=1e-12)
    assert verdict.decay_points == pytest.approx(points, rel=1e-12)
    assert verdict.underlying_price == forward


def test_the_time_is_calendar_time_to_the_expiry_moment(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    saturday = datetime(2026, 9, 19, 7, 0, tzinfo=PT).astimezone(UTC)
    monday = date(2026, 9, 21)
    q = quote(expiry=monday)
    exit_, _ = make(chain_of(q, ts=saturday))
    verdict = check(exit_, q, D(500), now=saturday)
    assert verdict.years == pytest.approx((expiry_moment(monday, instrument) - saturday) / YEAR)
    assert verdict.decay_points == pytest.approx(
        expected(q, saturday, settings, instrument)[1], rel=1e-12
    )


def test_a_negative_modelled_decay_never_says_out(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    years = (expiry_moment(WED, instrument) - NOW) / YEAR
    worth = price(
        Right76.PUT,
        forward=float(FORWARD),
        strike=22000.0,
        years=years,
        vol=0.3,
        rate=float(settings.rate),
    )
    q = quote(strike=22000, right=Right.PUT, bid=worth, ask=worth)
    exit_, _ = make(chain_of(q))
    verdict = check(exit_, q, D("0.01"))
    assert verdict.decay_dollars is not None
    assert verdict.decay_dollars < 0
    assert not verdict.fire


# --- cannot be valued: never sell on a guess --------------------------------------


def test_no_chain_stays(make: Callable[[OptionChain | None], tuple[DecayExit, Chains]]) -> None:
    exit_, chains = make(None)
    verdict = check(exit_, quote(), D(10))
    assert not verdict.fire
    assert "no option chain" in verdict.reason
    assert verdict.unvalued
    assert chains.asked == [NOW]


def test_the_contract_missing_from_the_chain_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
) -> None:
    exit_, _ = make(chain_of(quote(strike=20100)))
    verdict = check(exit_, quote(), D(10))
    assert not verdict.fire
    assert "not in the option chain" in verdict.reason
    assert verdict.unvalued


def test_a_stale_chain_stays_and_one_just_fresh_enough_is_valued(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    execution: ExecutionProfile,
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    q = quote()
    age = timedelta(seconds=execution.quotes.max_age_seconds)
    stale, _ = make(chain_of(q, ts=NOW - age - SEC))
    verdict = check(stale, q, D(10))
    assert not verdict.fire
    assert "stale" in verdict.reason
    assert verdict.decay_dollars is None
    assert verdict.unvalued
    fresh, _ = make(chain_of(q, ts=NOW - age))
    valued = check(fresh, q, D(10))
    assert valued.decay_dollars is not None
    assert valued.fire
    # Valued from now, not from the chain's own (older) stamp (mutation survivor 25).
    moment = expiry_moment(q.expiry, instrument)
    assert valued.years == (moment - NOW) / YEAR
    assert valued.decay_points == pytest.approx(expected(q, NOW, settings, instrument)[1])


def test_a_chain_from_the_future_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
) -> None:
    q = quote()
    exit_, _ = make(chain_of(q, ts=NOW + SEC))
    verdict = check(exit_, q, D(10))
    assert not verdict.fire
    assert "future" in verdict.reason
    assert verdict.decay_dollars is None
    assert verdict.unvalued


def test_a_quote_no_volatility_can_produce_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
) -> None:
    q = quote(strike=19000, bid=900, ask=905)  # under its intrinsic value
    exit_, _ = make(chain_of(q))
    verdict = check(exit_, q, D(10))
    assert not verdict.fire
    assert "implied volatility" in verdict.reason
    assert verdict.implied_vol is None
    assert verdict.unvalued


def test_implied_volatility_above_the_maximum_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    years = (expiry_moment(WED, instrument) - NOW) / YEAR
    rich = price(
        Right76.CALL,
        forward=float(FORWARD),
        strike=20000.0,
        years=years,
        vol=float(settings.max_implied_vol) * 1.2,
        rate=0.0,
    )
    q = quote(bid=round(rich), ask=round(rich) + 2)
    exit_, _ = make(chain_of(q))
    verdict = check(exit_, q, D(10))
    assert not verdict.fire
    assert str(settings.max_implied_vol) in verdict.reason


@pytest.mark.parametrize(("trading_class", "why"), [("NQ", "American"), ("ZZZ", "European")])
def test_a_class_black76_does_not_value_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]], trading_class: str, why: str
) -> None:
    q = quote(trading_class=trading_class)
    exit_, _ = make(chain_of(q))
    verdict = check(exit_, q, D(10))
    assert not verdict.fire
    assert why in verdict.reason
    assert verdict.decay_dollars is None
    assert verdict.unvalued


def test_near_expiry_within_the_window_it_stays_and_just_outside_it_is_valued(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    settings: ThetaSettings,
    instrument: InstrumentProfile,
) -> None:
    moment = expiry_moment(WED, instrument)
    window = timedelta(minutes=settings.decay_exit.window_minutes)
    q = quote(bid=20, ask=21)
    at, _ = make(chain_of(q, ts=moment - window))
    exactly = check(at, q, D(10), now=moment - window)
    assert not exactly.fire
    assert "expires" in exactly.reason
    assert str(settings.decay_exit.window_minutes) in exactly.reason
    assert exactly.decay_dollars is None
    before = moment - window - SEC
    just, _ = make(chain_of(q, ts=before))
    valued = check(just, q, D(300), now=before)
    assert valued.decay_points == pytest.approx(
        expected(q, before, settings, instrument)[1], rel=1e-9
    )
    # Near expiry the window's decay is most of what is left of the time value.
    assert valued.fire, valued.reason


def test_an_expired_option_stays(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    instrument: InstrumentProfile,
) -> None:
    after = expiry_moment(WED, instrument) + timedelta(minutes=1)
    q = quote()
    exit_, _ = make(chain_of(q, ts=after))
    verdict = check(exit_, q, D(10), now=after)
    assert not verdict.fire
    assert verdict.years is None


def test_inputs_the_model_cannot_take_stay(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
) -> None:
    q = quote()
    exit_, _ = make(chain_of(q, forward=D("1e400")))
    verdict = check(exit_, q, D(10))
    assert not verdict.fire
    assert "cannot value" in verdict.reason
    assert verdict.unvalued


def test_the_time_must_be_utc(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
) -> None:
    exit_, _ = make(chain_of(quote()))
    with pytest.raises(ValueError, match="now"):
        check(exit_, quote(), D(10), now=NOW.astimezone(PT))


def test_turned_off_it_never_says_out_and_values_nothing(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> None:
    q = quote()
    off = replace(settings, decay_exit=replace(settings.decay_exit, enabled=False))
    chains = Chains(chain_of(q))
    verdict = check(exit_for(off, instrument, risk, execution, chains), q, D("0.01"))
    assert not verdict.fire
    assert "off" in verdict.reason
    assert verdict.decay_dollars is None
    assert not verdict.unvalued  # off is not a failure to value
    assert chains.asked == []


def test_it_ships_off_until_andre_rules_on_dx2(
    strategy: StrategyProfile,
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> None:
    """Review H1: with the drafted numbers the decay exit closes every normally paced
    winner on its first small gain (DX-2), so the shipped default is off."""
    assert strategy.theta.decay_exit.enabled is False
    q = quote()
    decay = dollars(expected(q, NOW, settings, instrument)[1], instrument)
    chains = Chains(chain_of(q))
    shipped = exit_for(strategy.theta, instrument, risk, execution, chains)
    verdict = check(shipped, q, decay)  # fires when switched on (first test above)
    assert not verdict.fire
    assert chains.asked == []


def test_the_decay_dollars_use_the_instruments_point_value(
    settings: ThetaSettings,
    instrument: InstrumentProfile,
    risk: RiskProfile,
    execution: ExecutionProfile,
) -> None:
    """Review L2: the decay in dollars is the option points times THIS instrument's
    point value (a profile with another point value gives other dollars)."""
    q = quote()
    other = replace(instrument, point_value=instrument.point_value * 3)
    points = expected(q, NOW, settings, other)[1]
    exit_ = exit_for(settings, other, risk, execution, Chains(chain_of(q)))
    verdict = check(exit_, q, D(500), contracts=2)
    assert verdict.decay_dollars == D(str(points)) * other.point_value * 2
    assert verdict.decay_dollars != D(str(points)) * instrument.point_value * 2
    assert f"{verdict.decay_dollars:.2f}" in verdict.reason


def test_an_option_expiring_within_the_window_is_unvalued(
    make: Callable[[OptionChain | None], tuple[DecayExit, Chains]],
    instrument: InstrumentProfile,
) -> None:
    after = expiry_moment(WED, instrument) + timedelta(minutes=1)
    q = quote()
    exit_, _ = make(chain_of(q, ts=after))
    verdict = check(exit_, q, D(10), now=after)
    assert not verdict.fire
    assert verdict.unvalued
