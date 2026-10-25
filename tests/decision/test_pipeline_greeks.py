"""The Greeks vetoes inside the entry pipeline (GV-1): Vega, Delta and Gamma run after
Theta, in that order, and before Dolores' full check. A veto means no approved entry,
a reason on the record, and nothing after it runs; an ApprovedEntry can never carry a
vetoed or a mismatched Greeks check."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from footprint.agents.greeks import DeltaAgent, GammaAgent, GreekCheck, VegaAgent
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.pipeline import ApprovedEntry, Decision, Pipeline
from footprint.decision.risk.gate import AccountState
from footprint.execution.journal.josephine import Journal
from tests.conftest import REPO_ROOT
from tests.decision.plays import open_play
from tests.decision.scenario import DECISION_TIME, chain_for, market

D = Decimal
GREEKS = ("vega", "delta", "gamma")


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="module")
def up_market() -> list[Bar]:
    return market()


def clean(profiles: Profiles) -> AccountState:
    bankroll = profiles.risk.bankroll
    return AccountState(bankroll, bankroll, D(0), D(0), 0, 0, 0, False, False)


def chain(bars: list[Bar]) -> Any:
    closed = [b for b in bars if b.ts + timedelta(minutes=1) <= DECISION_TIME]
    return chain_for(closed[-1].close, DECISION_TIME)


def decide(profiles: Profiles, bars: list[Bar]) -> Decision:
    return Pipeline(profiles, play=open_play()).decide(
        bars, chain(bars), clean(profiles), (), DECISION_TIME
    )


def strict(profiles: Profiles, agent: str) -> Profiles:
    """The same profiles with `agent`'s rule set so no option of the scenario passes it."""
    s = profiles.strategy
    if agent == "vega":  # the scenario's volatility is under the band; still inside theta's
        changed = replace(s, vega=replace(s.vega, min_implied_vol=D("0.9")))
    elif agent == "delta":  # an at-the-money option is far from this band
        changed = replace(s, delta=replace(s.delta, min_abs_delta=D("0.95"), max_abs_delta=D(1)))
    else:  # every option of the scenario expires within this many hours
        changed = replace(s, gamma=replace(s.gamma, min_hours_to_expiry=D(1000)))
    return replace(profiles, strategy=changed)


def stingy_theta(profiles: Profiles) -> Profiles:
    theta = replace(profiles.strategy.theta, max_decay_pct_of_target=D("0.0001"))
    return replace(profiles, strategy=replace(profiles.strategy, theta=theta))


def test_a_fired_entry_carries_every_allowed_greeks_check(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    decision = decide(profiles, up_market)
    assert decision.fired, decision.reasons()
    assert decision.approved is not None
    for name in GREEKS:
        check = getattr(decision, name)
        assert isinstance(check, GreekCheck)
        assert check.agent_id == name
        assert check.allowed, check.reason
        assert getattr(decision.approved, name) is check
        assert check.implied_vol is not None
        assert check.delta is not None


def test_the_greeks_judge_the_contract_and_size_the_pipeline_chose(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    decision = decide(profiles, up_market)
    assert decision.approved is not None
    plan = decision.approved.plan
    for name in GREEKS:
        check = getattr(decision, name)
        assert (check.contract_id, check.contracts) == (
            plan.choice.quote.contract_id,
            plan.contracts,
        )


def test_the_greeks_value_against_the_chains_underlying_price(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    c = chain(up_market)
    decision = Pipeline(profiles, play=open_play()).decide(
        up_market, c, clean(profiles), (), DECISION_TIME
    )
    assert decision.approved is not None
    s, inst = profiles.strategy, profiles.instrument
    plan = decision.approved.plan
    direct = (
        VegaAgent(s.vega, s.theta.rate, inst),
        DeltaAgent(s.delta, s.vega, s.theta.rate, inst),
        GammaAgent(s.gamma, s.vega, s.theta.rate, inst),
    )
    for name, agent in zip(GREEKS, direct, strict=True):
        assert getattr(decision, name) == agent.assess(plan, c.underlying_price, DECISION_TIME)


@pytest.mark.parametrize("vetoing", GREEKS)
def test_a_greeks_veto_stops_the_entry_before_the_full_risk_check(
    profiles: Profiles, up_market: list[Bar], vetoing: str
) -> None:
    decision = decide(strict(profiles, vetoing), up_market)
    assert not decision.fired
    assert decision.approved is None
    assert decision.plan is not None  # Felix and Ezra ran
    assert decision.theta is not None
    assert decision.theta.allowed  # Theta ran first and passed
    assert decision.entry is None  # Dolores' full check did not run
    assert decision.risk is None
    check = getattr(decision, vetoing)
    assert check is not None
    assert not check.allowed
    order = GREEKS.index(vetoing)
    for earlier in GREEKS[:order]:
        assert getattr(decision, earlier).allowed
    for later in GREEKS[order + 1 :]:
        assert getattr(decision, later) is None
    vetoes = [r for r in decision.reasons() if r.split(":")[0] in GREEKS]
    assert vetoes == [f"{vetoing}: {check.reason}"]


def test_a_theta_veto_runs_no_greeks_check(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = decide(stingy_theta(profiles), up_market)
    assert decision.theta is not None
    assert not decision.theta.allowed
    for name in GREEKS:
        assert getattr(decision, name) is None


def test_no_greeks_check_runs_without_a_contract(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = Pipeline(profiles, play=open_play()).decide(
        up_market, None, clean(profiles), (), DECISION_TIME
    )
    for name in GREEKS:
        assert getattr(decision, name) is None
    assert not any(r.split(":")[0] in GREEKS for r in decision.reasons())


def test_allowed_greeks_are_not_reasons_when_risk_vetoes_later(
    profiles: Profiles, up_market: list[Bar], monkeypatch: pytest.MonkeyPatch
) -> None:
    from footprint.decision import pipeline
    from footprint.decision.risk.gate import RiskDecision, Veto
    from footprint.decision.session import SessionKind

    def vetoing(*args: object) -> RiskDecision:
        return RiskDecision(
            vetoes=(Veto.OVER_POSITION_CAP,),
            session=SessionKind.PRIME,
            blackout=None,
            latch_kill_switch=False,
            entry=args[-1],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(pipeline, "evaluate", vetoing)
    decision = decide(profiles, up_market)
    assert not decision.fired
    for name in GREEKS:
        assert getattr(decision, name).allowed
    assert not any(r.split(":")[0] in GREEKS for r in decision.reasons())
    assert "risk: OVER_POSITION_CAP" in decision.reasons()


def _approve(approved: ApprovedEntry, **changes: GreekCheck) -> ApprovedEntry:
    from footprint.decision import pipeline

    checks = {name: getattr(approved, name) for name in GREEKS} | changes
    return ApprovedEntry(
        approved.signal,
        approved.plan,
        approved.entry,
        approved.risk,
        approved.theta,
        checks["vega"],
        checks["delta"],
        checks["gamma"],
        approved.play,
        pipeline._APPROVAL,
    )


def test_the_approval_rebuilt_unchanged_is_accepted(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    _approve(approved)  # the control: the cases below fail only on their change


@pytest.mark.parametrize("name", GREEKS)
def test_an_approval_cannot_carry_a_vetoed_greeks_check(
    profiles: Profiles, up_market: list[Bar], name: str
) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    vetoed = replace(getattr(approved, name), allowed=False, reason="vetoed")
    with pytest.raises(PermissionError, match=f"{name} vetoed"):
        _approve(approved, **{name: vetoed})


@pytest.mark.parametrize("name", GREEKS)
def test_an_approval_cannot_carry_another_contracts_check(
    profiles: Profiles, up_market: list[Bar], name: str
) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    other = replace(getattr(approved, name), contract_id="other")
    with pytest.raises(PermissionError, match=f"{name} judged a different"):
        _approve(approved, **{name: other})


@pytest.mark.parametrize("name", GREEKS)
def test_an_approval_cannot_carry_a_check_for_another_size(
    profiles: Profiles, up_market: list[Bar], name: str
) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    check = getattr(approved, name)
    other = replace(check, contracts=check.contracts + 1)
    with pytest.raises(PermissionError, match=f"{name} judged a different"):
        _approve(approved, **{name: other})


@pytest.mark.parametrize("name", GREEKS)
def test_an_approval_cannot_carry_another_agents_check_in_a_slot(
    profiles: Profiles, up_market: list[Bar], name: str
) -> None:
    approved = decide(profiles, up_market).approved
    assert approved is not None
    other = next(n for n in GREEKS if n != name)
    with pytest.raises(PermissionError, match=f"not a {name} check"):
        _approve(approved, **{name: getattr(approved, other)})


@pytest.mark.parametrize("vetoing", GREEKS)
def test_the_journal_records_the_greeks_checks(
    profiles: Profiles, up_market: list[Bar], tmp_path: Path, vetoing: str
) -> None:
    journal = Journal(tmp_path / "journal")
    decision = decide(strict(profiles, vetoing), up_market)
    journal.record_decision(decision)
    (record,) = journal.decisions()
    check = getattr(decision, vetoing)
    assert record[vetoing]["allowed"] is False
    assert record[vetoing]["reason"] == check.reason
    assert record[vetoing]["agent_id"] == vetoing
    assert any(r.startswith(f"{vetoing}: ") for r in record["reasons"])
    json.dumps(record)  # plain JSON all the way down


def test_the_journal_records_the_numbers_of_a_fired_entry(
    profiles: Profiles, up_market: list[Bar], tmp_path: Path
) -> None:
    journal = Journal(tmp_path / "journal")
    decision = decide(profiles, up_market)
    journal.record_decision(decision)
    (record,) = journal.decisions()
    for name in GREEKS:
        check = getattr(decision, name)
        assert record[name]["allowed"] is True
        assert record[name]["implied_vol"] == check.implied_vol
        assert record[name]["delta"] == check.delta
        assert record[name]["gamma"] == check.gamma
        assert record[name]["vega"] == check.vega
