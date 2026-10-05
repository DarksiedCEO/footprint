"""Desmond (console) and the end-to-end entry pipeline, tested from the spec:
console weighting (hard gates, levels x2 + range, fire at +3), G13, G14, NB-14, NB-15."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from footprint.agents.levels import Level, LevelsReader, LevelsSnapshot, Unavailable
from footprint.agents.range_reader import RangeReader
from footprint.agents.zero_day import ZeroDayCheck
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar, Direction, Read, Score
from footprint.core.options import Right
from footprint.decision.console.desmond import Console, ConsoleInputs, ConsoleVerdict
from footprint.decision.pipeline import ApprovedEntry, Decision, Pipeline
from footprint.decision.risk.gate import AccountState, Veto
from footprint.decision.session import NewsBlackout
from tests.conftest import REPO_ROOT
from tests.decision.scenario import (
    DECISION_TIME,
    LAST_DAY,
    PT,
    at,
    chain_for,
    decision_time,
    market,
)

D = Decimal


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="module")
def up_market() -> list[Bar]:
    return market()


def clean(profiles: Profiles) -> AccountState:
    bankroll = profiles.risk.bankroll
    return AccountState(bankroll, bankroll, D(0), D(0), 0, 0, 0, False, False)


def decide(
    profiles: Profiles,
    bars: list[Bar],
    *,
    account: AccountState | None = None,
    blackouts: tuple[NewsBlackout, ...] = (),
    as_of: datetime = DECISION_TIME,
    with_chain: bool = True,
) -> Decision:
    closed = [b for b in bars if b.ts + timedelta(minutes=1) <= as_of]
    chain = chain_for(closed[-1].close, as_of) if with_chain and closed else None
    return Pipeline(profiles).decide(bars, chain, account or clean(profiles), blackouts, as_of)


# --- end to end -----------------------------------------------------------------


def test_everything_lined_up_fires_one_long(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = decide(profiles, up_market)
    assert decision.fired, decision.reasons()
    approved = decision.approved
    assert approved is not None
    signal = approved.signal
    assert signal.direction is Direction.LONG
    assert [r.agent_id for r in signal.reads] == ["trend", "momentum", "volume", "levels", "range"]
    assert all(r.ts <= signal.ts for r in signal.reads)
    assert decision.verdict is not None
    assert decision.verdict.score == 3
    assert approved.plan.choice.quote.right is Right.CALL
    assert approved.plan.contracts == 1
    assert approved.entry.cost == approved.plan.cost  # Dolores saw the real sized cost
    assert approved.risk.allowed
    assert decision.precheck.allowed
    assert decision.reasons() == []


def test_a_short_fires_in_a_downtrend(profiles: Profiles) -> None:
    decision = decide(profiles, market(mirror=True, bearish_spike=True))
    assert decision.fired, decision.reasons()
    assert decision.approved is not None
    assert decision.approved.signal.direction is Direction.SHORT
    assert decision.approved.plan.choice.quote.right is Right.PUT


def test_nb14_a_bullish_trigger_in_a_downtrend_is_blocked(profiles: Profiles) -> None:
    decision = decide(profiles, market(mirror=True))
    assert not decision.fired
    assert decision.verdict is not None
    assert "momentum_with_trend" in [g.name for g in decision.verdict.failed]
    assert decision.verdict.direction is Direction.SHORT
    assert decision.plan is None  # nothing downstream ran


def test_no_trigger_no_trade(profiles: Profiles) -> None:
    decision = decide(profiles, market(spike=False))
    assert not decision.fired
    assert decision.verdict is not None
    assert "momentum_with_trend" in [g.name for g in decision.verdict.failed]


def test_zero_day_trip_alone_blocks_the_trade(profiles: Profiles) -> None:
    decision = decide(profiles, market(spike_volume=1100))  # an 11x bar
    assert not decision.fired
    assert decision.verdict is not None
    assert [g.name for g in decision.verdict.failed] == ["zero_day_clear"]


def test_resistance_between_one_and_one_and_a_half_n_scores_zero(profiles: Profiles) -> None:
    n = profiles.risk.profit_target / (
        profiles.strategy.options.assumed_delta * profiles.instrument.point_value
    )
    decision = decide(profiles, market(wall_above=(n * D("1.25")).quantize(D("0.25"))))
    assert not decision.fired
    assert decision.verdict is not None
    levels = next(r for r in decision.verdict.reads if r.agent_id == "levels")
    assert levels.score is Score.NEUTRAL
    assert "prior_high" in levels.reason
    assert decision.verdict.score == 1  # levels 0 + range +1
    assert [g.name for g in decision.verdict.failed] == ["confirmer_score"]


def test_range_is_judged_after_the_cash_close(profiles: Profiles) -> None:
    late = 569  # 13:29: spike, 13:30 follow-through, decide at 13:31
    bars = market(spike_minute=late)
    decision = decide(profiles, bars, as_of=decision_time(late))
    assert decision.verdict is not None
    range_read = next(r for r in decision.verdict.reads if r.agent_id == "range")
    assert "not open" not in range_read.reason
    assert "of the 10-day ADR" in range_read.reason


def test_a_forming_bar_cannot_change_the_decision(profiles: Profiles, up_market: list[Bar]) -> None:
    last = up_market[-1]
    forming = Bar("NQ", DECISION_TIME, last.close, last.close + 500, D(1), D(2), 10**6)
    later = DECISION_TIME + timedelta(seconds=59)
    decision = decide(profiles, [*up_market, forming], as_of=later)
    assert decision.fired, decision.reasons()


def test_risk_precheck_is_always_on_the_record(profiles: Profiles) -> None:
    bankroll = profiles.risk.bankroll
    shut = AccountState(bankroll, bankroll, D(-2500), D(-4000), 2, 4, 0, True, True)
    decision = decide(profiles, market(spike=False), account=shut)
    assert not decision.fired
    vetoes = set(decision.precheck.vetoes)
    assert {
        Veto.KILL_SWITCH,
        Veto.DAILY_STOPOUTS,
        Veto.DAILY_LOSS,
        Veto.WEEKLY_LOSS,
        Veto.POSITION_OPEN,
        Veto.TRADE_CEILING,
    } <= vetoes
    assert "risk: KILL_SWITCH" in decision.reasons()


def test_a_precheck_veto_blocks_an_otherwise_perfect_setup(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    holding = replace(clean(profiles), position_open=True)
    decision = decide(profiles, up_market, account=holding)
    assert not decision.fired
    assert Veto.POSITION_OPEN in decision.precheck.vetoes
    assert decision.verdict is not None
    assert decision.verdict.go  # the setup itself was fine
    assert decision.plan is None  # nothing was sized


def test_news_blackout_blocks_the_signal(profiles: Profiles, up_market: list[Bar]) -> None:
    window = NewsBlackout("CPI", DECISION_TIME - timedelta(minutes=5), DECISION_TIME)
    decision = decide(profiles, up_market, blackouts=(window,))
    assert not decision.fired
    assert Veto.NEWS_BLACKOUT in decision.precheck.vetoes


def test_no_chain_no_trade(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = decide(profiles, up_market, with_chain=False)
    assert not decision.fired
    assert any("no option chain" in r for r in decision.reasons())


def test_kill_switch_latch_is_raised_even_without_a_setup(profiles: Profiles) -> None:
    bankroll = profiles.risk.bankroll
    down = AccountState(bankroll * D("0.8"), bankroll, D(0), D(0), 0, 0, 0, False, False)
    decision = decide(profiles, market(spike=False), account=down)
    assert not decision.fired
    assert decision.latch_kill_switch


def test_no_data_no_trade(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = decide(profiles, [], with_chain=False)
    assert not decision.fired
    thin = decide(profiles, up_market[-300:])  # hours, not weeks, of history
    assert not thin.fired


def test_malformed_feeds_are_refused_not_crashed_on(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    foreign = [replace(b, symbol="ES") for b in up_market]
    assert "not the traded instrument" in (decide(profiles, foreign).data_error or "")
    shuffled = [*up_market[:-2], up_market[-1], up_market[-2]]
    out_of_order = decide(profiles, shuffled)
    assert not out_of_order.fired
    assert out_of_order.data_error is not None


def test_only_the_pipeline_can_approve_an_entry(profiles: Profiles, up_market: list[Bar]) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    with pytest.raises(PermissionError, match="only the decision pipeline"):
        ApprovedEntry(approved.signal, approved.plan, approved.entry, approved.risk, object())


# --- console unit tests ---------------------------------------------------------


def read(agent: str, score: Score, ts: datetime = DECISION_TIME) -> Read:
    return Read(agent, ts, score, f"{agent} {score.name}")


def snapshot(price: float, vwap: float, *walls: tuple[str, float]) -> LevelsSnapshot:
    return LevelsSnapshot(
        as_of=DECISION_TIME,
        price=D(str(price)),
        vwap=D(str(vwap)),
        walls=tuple(Level(n, D(str(p))) for n, p in walls),
        round_below=D(0),
        round_above=D(0),
    )


def quiet_days(today_low: float = 1000, today_high: float = 1030, close: float = 1015) -> list[Bar]:
    start = at(LAST_DAY, 150) - timedelta(days=14)
    history = [
        Bar("NQ", start + timedelta(days=i), D(1000), D(1100), D(1000), D(1050), 100)
        for i in range(10)
    ]
    today = Bar(
        "NQ",
        at(LAST_DAY, 150),
        D(str(today_low)),
        D(str(today_high)),
        D(str(today_low)),
        D(str(close)),
        100,
    )
    return [*history, today]


CALM = ZeroDayCheck(DECISION_TIME, False, ("calm",))


def inputs(**changes: object) -> ConsoleInputs:
    base = ConsoleInputs(
        as_of=DECISION_TIME,
        trend=read("trend", Score.BULLISH),
        momentum=read("momentum", Score.BULLISH),
        volume=read("volume", Score.NEUTRAL),
        zero_day=CALM,
        levels=snapshot(1015, 1010),
        range_days=quiet_days(),
        n_points=D(100),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def desmond(profiles: Profiles) -> Console:
    s = profiles.strategy
    levels = LevelsReader(s.levels, s.trend, s.data, profiles.instrument)
    return Console(s.console, profiles.instrument, levels, RangeReader(s.range))


def failed(verdict: ConsoleVerdict) -> list[str]:
    return [g.name for g in verdict.failed]


def test_console_goes_when_every_gate_passes(desmond: Console) -> None:
    verdict = desmond.evaluate(inputs())
    assert verdict.go, verdict.failed
    assert (verdict.direction, verdict.score) == (Direction.LONG, 3)


def test_g14_neutral_trend_is_no_trade(desmond: Console) -> None:
    verdict = desmond.evaluate(inputs(trend=read("trend", Score.NEUTRAL)))
    assert not verdict.go
    assert verdict.direction is None
    assert {"trend_has_direction", "momentum_with_trend"} <= set(failed(verdict))


@pytest.mark.parametrize(
    ("trend", "momentum"),
    [
        (Score.BULLISH, Score.BEARISH),
        (Score.BEARISH, Score.BULLISH),
        (Score.BULLISH, Score.NEUTRAL),
    ],
)
def test_nb14_momentum_must_point_with_the_trend(
    desmond: Console, trend: Score, momentum: Score
) -> None:
    verdict = desmond.evaluate(
        inputs(trend=read("trend", trend), momentum=read("momentum", momentum))
    )
    assert not verdict.go
    if trend is Score.BULLISH:
        assert failed(verdict) == ["momentum_with_trend"]  # the only reason
    else:
        assert "momentum_with_trend" in failed(verdict)


@pytest.mark.parametrize(
    ("direction", "price", "vwap", "ok"),
    [
        (Score.BULLISH, 1015, 1010, True),
        (Score.BULLISH, 1010, 1015, False),  # long below VWAP
        (Score.BULLISH, 1010, 1010, False),  # long at VWAP: not above
        (Score.BEARISH, 985, 990, True),
        (Score.BEARISH, 995, 990, False),  # short above VWAP
    ],
)
def test_nb15_vwap_side(
    desmond: Console, direction: Score, price: float, vwap: float, ok: bool
) -> None:
    low, high = (1000, 1030) if direction is Score.BULLISH else (970, 1000)
    verdict = desmond.evaluate(
        inputs(
            trend=read("trend", direction),
            momentum=read("momentum", direction),
            levels=snapshot(price, vwap),
            range_days=quiet_days(low, high, (low + high) / 2),
        )
    )
    assert ("vwap_side" not in failed(verdict)) is ok


def test_zero_day_trip_stands_the_system_down(desmond: Console) -> None:
    tripped = ZeroDayCheck(DECISION_TIME, True, ("range: 9x typical",))
    verdict = desmond.evaluate(inputs(zero_day=tripped))
    assert not verdict.go
    assert failed(verdict) == ["zero_day_clear"]


def test_a_wall_before_the_target_vetoes(desmond: Console) -> None:
    verdict = desmond.evaluate(inputs(levels=snapshot(1015, 1010, ("prior_high", 1050))))
    assert not verdict.go
    assert "levels_room" in failed(verdict)


def test_unavailable_levels_veto_and_fail_the_vwap_gate(desmond: Console) -> None:
    verdict = desmond.evaluate(inputs(levels=Unavailable(DECISION_TIME, "stale feed")))
    assert {"levels_room", "vwap_side", "confirmer_score"} <= set(failed(verdict))


def test_g13_both_confirmers_must_be_positive(desmond: Console) -> None:
    tight = desmond.evaluate(inputs(levels=snapshot(1015, 1010, ("prior_high", 1140))))  # 1.25N
    assert tight.score == 1  # levels 0 + range +1
    assert failed(tight) == ["confirmer_score"]
    busy = desmond.evaluate(inputs(range_days=quiet_days(1000, 1080, 1040)))  # 80% of ADR
    assert busy.score == 2
    assert failed(busy) == ["confirmer_score"]
    closed = desmond.evaluate(inputs(range_days=None))
    assert closed.score == 2
    assert not closed.go


def test_volume_is_not_scored(desmond: Console) -> None:
    for volume in Score:
        assert desmond.evaluate(inputs(volume=read("volume", volume))).score == 3


@pytest.mark.parametrize(
    ("hour", "minute", "gate"),
    [
        (13, 54, None),
        (13, 55, "before_close_lead"),  # 5 minutes before the 14:00 close
        (14, 30, "prime_session"),
        (20, 0, "prime_session"),  # overnight never trades in v1
    ],
)
def test_time_gates(desmond: Console, hour: int, minute: int, gate: str | None) -> None:
    when = datetime(2026, 9, 15, hour, minute, tzinfo=PT).astimezone(DECISION_TIME.tzinfo)
    verdict = desmond.evaluate(inputs(as_of=when))
    if gate is None:
        assert verdict.go, verdict.failed
    else:
        assert gate in failed(verdict)


def test_a_negative_range_read_subtracts(desmond: Console) -> None:
    extended = desmond.evaluate(inputs(range_days=quiet_days(1000, 1101, 1050)))  # 101% of ADR
    assert extended.score == 1  # levels +2, range -1
    chasing = desmond.evaluate(inputs(range_days=quiet_days(1000, 1080, 1076)))  # top 10%, 80%
    assert chasing.score == 1
    range_read = next(r for r in chasing.reads if r.agent_id == "range")
    assert "chasing" in range_read.reason
