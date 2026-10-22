"""The decay veto inside the entry pipeline: Theta runs after Felix and Ezra and before
Dolores' full check; a vetoed Theta means no approved entry, a reason on the record, and
an ApprovedEntry can never carry a vetoed Theta."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.agents.theta import ThetaCheck
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.pipeline import ApprovedEntry, Decision, Pipeline
from footprint.decision.risk.gate import AccountState
from footprint.execution.journal.josephine import Journal
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, chain_for, market

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


def decide(profiles: Profiles, bars: list[Bar]) -> Decision:
    closed = [b for b in bars if b.ts + timedelta(minutes=1) <= DECISION_TIME]
    chain = chain_for(closed[-1].close, DECISION_TIME)
    return Pipeline(profiles).decide(bars, chain, clean(profiles), (), DECISION_TIME)


def stingy(profiles: Profiles) -> Profiles:
    """The same profiles with a decay budget no real option can meet."""
    theta = replace(profiles.strategy.theta, max_decay_pct_of_target=D("0.0001"))
    return replace(profiles, strategy=replace(profiles.strategy, theta=theta))


def test_a_fired_entry_carries_the_allowed_theta_check(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    decision = decide(profiles, up_market)
    assert decision.fired, decision.reasons()
    assert isinstance(decision.theta, ThetaCheck)
    assert decision.theta.allowed
    assert decision.approved is not None
    assert decision.approved.theta is decision.theta
    assert decision.theta.decay_dollars is not None
    assert decision.theta.decay_dollars > 0


def test_theta_judges_the_contract_and_size_the_pipeline_chose(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    decision = decide(profiles, up_market)
    assert decision.approved is not None
    assert decision.theta is not None
    plan = decision.approved.plan
    assert decision.theta.contract_id == plan.choice.quote.contract_id
    assert decision.theta.contracts == plan.contracts
    assert decision.theta.decay_points is not None
    points = D(str(decision.theta.decay_points))
    assert decision.theta.decay_dollars == points * profiles.instrument.point_value * plan.contracts
    assert decision.theta.expires_at is not None
    assert decision.theta.expires_at.date() >= plan.choice.quote.expiry


def test_theta_values_against_the_chains_underlying_price(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    from footprint.agents.theta import ThetaAgent

    closed = [b for b in up_market if b.ts + timedelta(minutes=1) <= DECISION_TIME]
    chain = chain_for(closed[-1].close, DECISION_TIME)
    decision = Pipeline(profiles).decide(up_market, chain, clean(profiles), (), DECISION_TIME)
    assert decision.approved is not None
    direct = ThetaAgent(profiles.strategy.theta, profiles.instrument, profiles.risk).assess(
        decision.approved.plan, chain.underlying_price, DECISION_TIME
    )
    assert decision.theta == direct


def test_an_allowed_theta_is_not_a_reason_when_risk_vetoes_later(
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
    assert decision.theta is not None
    assert decision.theta.allowed
    assert decision.risk is not None
    assert not any(r.startswith("theta") for r in decision.reasons())
    assert "risk: OVER_POSITION_CAP" in decision.reasons()


def test_a_theta_veto_stops_the_entry_before_the_full_risk_check(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    decision = decide(stingy(profiles), up_market)
    assert not decision.fired
    assert decision.approved is None
    assert decision.theta is not None
    assert not decision.theta.allowed
    assert decision.plan is not None  # Felix and Ezra ran
    assert decision.risk is None
    assert decision.entry is None  # Dolores' full check did not
    theta_reasons = [r for r in decision.reasons() if r.startswith("theta: ")]
    assert theta_reasons == [f"theta: {decision.theta.reason}"]


def test_no_theta_check_runs_without_a_contract(profiles: Profiles, up_market: list[Bar]) -> None:
    decision = Pipeline(profiles).decide(up_market, None, clean(profiles), (), DECISION_TIME)
    assert decision.theta is None
    assert not any(r.startswith("theta") for r in decision.reasons())


def test_an_approval_cannot_carry_a_vetoed_theta(profiles: Profiles, up_market: list[Bar]) -> None:
    from footprint.decision import pipeline

    approved = decide(profiles, up_market).approved
    assert approved is not None
    vetoed = replace(approved.theta, allowed=False, reason="vetoed")
    with pytest.raises(PermissionError, match="theta"):
        ApprovedEntry(
            approved.signal,
            approved.plan,
            approved.entry,
            approved.risk,
            vetoed,
            pipeline._APPROVAL,
        )


def test_an_approval_cannot_carry_another_plans_theta(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    from footprint.decision import pipeline

    approved = decide(profiles, up_market).approved
    assert approved is not None
    other_quote = replace(approved.plan.choice.quote, contract_id="other")
    other = replace(approved.theta, contract_id=other_quote.contract_id)
    with pytest.raises(PermissionError, match="theta"):
        ApprovedEntry(
            approved.signal,
            approved.plan,
            approved.entry,
            approved.risk,
            other,
            pipeline._APPROVAL,
        )


def test_an_approval_cannot_carry_a_theta_for_another_size(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    from footprint.decision import pipeline

    approved = decide(profiles, up_market).approved
    assert approved is not None
    other = replace(approved.theta, contracts=approved.plan.contracts + 1)
    with pytest.raises(PermissionError, match="theta"):
        ApprovedEntry(
            approved.signal,
            approved.plan,
            approved.entry,
            approved.risk,
            other,
            pipeline._APPROVAL,
        )


def test_the_journal_records_the_theta_check(
    profiles: Profiles, up_market: list[Bar], tmp_path: Path
) -> None:
    journal = Journal(tmp_path / "journal")
    decision = decide(stingy(profiles), up_market)
    journal.record_decision(decision)
    (record,) = journal.decisions()
    assert record["theta"]["allowed"] is False
    assert record["theta"]["reason"] == decision.theta.reason  # type: ignore[union-attr]
    assert any(r.startswith("theta: ") for r in record["reasons"])
    json.dumps(record)  # plain JSON all the way down
