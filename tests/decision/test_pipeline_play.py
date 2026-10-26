"""The play caller inside the entry pipeline (PC-5, PC-4).

Written before the code. The pipeline asks the play caller on EVERY decision, first, so
the day's label is made at the decision minute whatever else happens (a data error, a
risk veto, a failed gate). Only a TREND play lets an entry through; any other play means
no entry, with a "play:" reason on the record. The crew still reads, so the record shows
what it saw. An ApprovedEntry can never carry a play that takes no entries. Pipeline(p),
as live builds it, is the real play caller: on the short synthetic market (26 sessions,
far less than the 61 the reader needs) it never fires.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.agents.regime_reader import Regime
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.pipeline import ApprovedEntry, Decision, Pipeline
from footprint.decision.play import PlayCall, PlayCause
from footprint.decision.risk.gate import AccountState
from footprint.execution.journal.josephine import Journal
from tests.conftest import REPO_ROOT
from tests.decision.plays import FixedPlay
from tests.decision.scenario import DECISION_TIME, chain_for, market

D = Decimal
MINUTE = timedelta(minutes=1)


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
    pipeline: Pipeline,
    profiles: Profiles,
    bars: list[Bar],
    account: AccountState | None = None,
) -> Decision:
    closed = [b for b in bars if b.ts + MINUTE <= DECISION_TIME]
    chain = chain_for(closed[-1].close, DECISION_TIME)
    return pipeline.decide(bars, chain, account or clean(profiles), (), DECISION_TIME)


def test_a_trend_play_fires_as_before(profiles: Profiles, up_market: list[Bar]) -> None:
    play = FixedPlay(PlayCause.TREND)
    decision = decide(Pipeline(profiles, play=play), profiles, up_market)
    assert decision.fired, decision.reasons()
    assert decision.play is not None
    assert decision.play.entries_allowed
    assert decision.approved is not None
    assert decision.approved.play is decision.play
    assert decision.reasons() == []
    assert play.calls == [DECISION_TIME]


@pytest.mark.parametrize(
    "cause",
    [c for c in PlayCause if c is not PlayCause.TREND],
)
def test_any_other_play_takes_no_entry(
    profiles: Profiles, up_market: list[Bar], cause: PlayCause
) -> None:
    decision = decide(Pipeline(profiles, play=FixedPlay(cause)), profiles, up_market)
    assert not decision.fired
    assert decision.approved is None
    assert decision.play is not None
    assert decision.play.cause is cause
    assert f"play: test stand-in: {cause.value}" in decision.reasons()
    # The crew still read and the console still judged: the record shows a go it refused.
    assert decision.verdict is not None
    assert decision.verdict.go
    # Nothing downstream of the console ran.
    assert decision.contract is None
    assert decision.plan is None
    assert decision.theta is None
    assert decision.entry is None
    assert decision.risk is None


def test_the_play_is_asked_even_when_the_data_is_malformed(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    play = FixedPlay(PlayCause.TREND)
    shuffled = [*up_market[:-2], up_market[-1], up_market[-2]]
    decision = decide(Pipeline(profiles, play=play), profiles, shuffled)
    assert decision.data_error is not None
    assert not decision.fired
    assert play.calls == [DECISION_TIME]
    assert decision.play is not None


def test_the_play_is_asked_even_when_risk_says_no(profiles: Profiles, up_market: list[Bar]) -> None:
    play = FixedPlay(PlayCause.TREND)
    latched = replace(clean(profiles), kill_switch_latched=True)
    decision = decide(Pipeline(profiles, play=play), profiles, up_market, latched)
    assert not decision.fired
    assert not decision.precheck.allowed
    assert play.calls == [DECISION_TIME]


def test_a_blocking_play_and_a_risk_veto_are_both_on_the_record(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    latched = replace(clean(profiles), kill_switch_latched=True)
    decision = decide(
        Pipeline(profiles, play=FixedPlay(PlayCause.CHOP)), profiles, up_market, latched
    )
    reasons = decision.reasons()
    assert any(r.startswith("risk: ") for r in reasons)
    assert "play: test stand-in: chop" in reasons
    assert reasons.index("play: test stand-in: chop") == len(decision.precheck.vetoes)


def test_the_default_pipeline_is_the_real_play_caller(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    """Live builds Pipeline(profiles). The scenario's decision (07:01 PT) is the first of
    its day and comes after the decision minute (07:00 PT): LATE, no entry. Even on time
    the 26 sessions could never be labelled."""
    decision = decide(Pipeline(profiles), profiles, up_market)
    assert not decision.fired
    assert decision.play is not None
    assert decision.play.cause is PlayCause.LATE
    assert decision.play.first
    assert any(r.startswith("play: no label on time") for r in decision.reasons())


def test_the_default_play_caller_on_time_stands_aside_on_too_little_history(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    on_time = DECISION_TIME - MINUTE  # 07:00 PT: the decision minute
    bars = [b for b in up_market if b.ts + MINUTE <= on_time]
    decision = Pipeline(profiles).decide(
        bars, chain_for(bars[-1].close, on_time), clean(profiles), (), on_time
    )
    assert decision.play is not None
    assert decision.play.cause is PlayCause.NO_DATA
    assert not decision.fired


def test_an_approval_cannot_carry_a_play_that_takes_no_entries(
    profiles: Profiles, up_market: list[Bar]
) -> None:
    from footprint.decision import pipeline

    approved = decide(
        Pipeline(profiles, play=FixedPlay(PlayCause.TREND)), profiles, up_market
    ).approved
    assert approved is not None
    for cause in (PlayCause.CHOP, PlayCause.RULES, PlayCause.LATE, PlayCause.WARM_UP):
        regime = Regime.CHOP if cause is PlayCause.CHOP else Regime.STAND_ASIDE
        blocked = PlayCall(approved.play.day, regime, cause, "no")
        with pytest.raises(PermissionError, match="only a TREND day"):
            ApprovedEntry(
                approved.signal,
                approved.plan,
                approved.entry,
                approved.risk,
                approved.theta,
                approved.vega,
                approved.delta,
                approved.gamma,
                blocked,
                pipeline._APPROVAL,
            )


def test_the_journal_records_the_days_play_once(
    profiles: Profiles, up_market: list[Bar], tmp_path: Path
) -> None:
    journal = Journal(tmp_path)
    pipeline = Pipeline(profiles)
    decision = decide(pipeline, profiles, up_market)
    journal.record_decision(decision)
    later = pipeline.decide(
        up_market, chain_for(up_market[-1].close), clean(profiles), (), DECISION_TIME + MINUTE
    )
    assert later.play is not None
    assert not later.play.first
    journal.record_decision(later)
    plays = journal.plays()
    assert len(plays) == 1
    record = plays[0]
    assert record["cause"] == "late"
    assert record["regime"] == "stand_aside"
    assert record["entries_allowed"] is False
    assert record["day"] == DECISION_TIME.date().isoformat()
    assert record["as_of"] == DECISION_TIME.isoformat()
    assert record["reason"] == decision.play.reason  # type: ignore[union-attr]
    assert record["label"] is None
    assert len(journal.decisions()) == 2
    raw = (tmp_path / "plays.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(raw[0]) == record


def test_the_journal_keeps_the_readers_label(profiles: Profiles, tmp_path: Path) -> None:
    from footprint.decision.play import PlayCaller
    from tests.agents.test_regime_reader import SMALL, _decision
    from tests.decision.test_play import trend_day

    inst = profiles.instrument
    caller = PlayCaller(SMALL, profiles.play, profiles.strategy.data, inst)
    bars = trend_day(inst)
    at = _decision(inst)
    decision = Pipeline(profiles, play=caller).decide(bars, None, clean(profiles), (), at)
    assert decision.play is not None
    assert decision.play.cause is PlayCause.TREND
    journal = Journal(tmp_path)
    journal.record_decision(decision)
    (record,) = journal.plays()
    assert record["cause"] == "trend"
    assert record["entries_allowed"] is True
    assert record["label"]["regime"] == "trend"
    assert record["label"]["efficiency_rank_pct"] is not None


def test_no_play_record_without_a_play(
    profiles: Profiles, up_market: list[Bar], tmp_path: Path
) -> None:
    journal = Journal(tmp_path)
    decision = decide(Pipeline(profiles, play=FixedPlay(PlayCause.TREND)), profiles, up_market)
    journal.record_decision(replace(decision, play=None))
    journal.record_decision(decision)  # a stand-in's play is never `first`
    assert journal.plays() == []


# --- review round 1 ---------------------------------------------------------------------


def _trend_decisions(profiles: Profiles) -> tuple[Decision, Decision]:
    """The real play caller (small lookbacks) on a trend day: the decision that fixes the
    day's label, and a later one the same day."""
    from footprint.decision.play import PlayCaller
    from tests.agents.test_regime_reader import SMALL, _decision
    from tests.decision.test_play import trend_day

    inst = profiles.instrument
    pipeline = Pipeline(
        profiles, play=PlayCaller(SMALL, profiles.play, profiles.strategy.data, inst)
    )
    bars = trend_day(inst)
    at = _decision(inst)
    first = pipeline.decide(bars, None, clean(profiles), (), at)
    later = pipeline.decide(bars, None, clean(profiles), (), at + MINUTE)
    assert first.play is not None
    assert first.play.first
    assert later.play is not None
    assert not later.play.first
    return first, later


class _FailPlays:
    """Journal._append that fails on the plays file `times` times."""

    def __init__(self, times: int) -> None:
        self.times = times
        self.real = Journal._append

    def __call__(self, path: Path, record: dict[str, object]) -> None:
        if path.name == "plays.jsonl" and self.times > 0:
            self.times -= 1
            raise OSError("disk full")
        self.real(path, record)


def test_a_failed_play_record_is_written_on_the_next_decision(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3: the day's label is journaled from the decision that fixed it; if that write
    fails, it is retried on the next decision (and the failure still raises, so the
    runner alarms it). The decision record itself is never lost to it."""
    first, later = _trend_decisions(profiles)
    journal = Journal(tmp_path)
    monkeypatch.setattr(Journal, "_append", staticmethod(_FailPlays(1)))
    with pytest.raises(OSError, match="disk full"):
        journal.record_decision(first)
    assert journal.plays() == []
    assert len(journal.decisions()) == 1  # written despite the plays failure
    journal.record_decision(later)
    (record,) = journal.plays()
    assert record["cause"] == "trend"
    assert record["as_of"] == first.as_of.isoformat()  # the fixing decision's record
    journal.record_decision(later)
    assert len(journal.plays()) == 1  # once a day
    assert len(journal.decisions()) == 3


def test_a_play_record_failing_twice_is_retried_until_written(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, later = _trend_decisions(profiles)
    journal = Journal(tmp_path)
    monkeypatch.setattr(Journal, "_append", staticmethod(_FailPlays(2)))
    with pytest.raises(OSError, match="disk full"):
        journal.record_decision(first)
    with pytest.raises(OSError, match="disk full"):
        journal.record_decision(later)
    journal.record_decision(later)
    assert len(journal.plays()) == 1
    assert len(journal.decisions()) == 3


def test_every_owed_play_record_is_written_on_the_next_success(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record still owed when the next day's label is fixed: both are written, the
    owed one first."""
    first, _ = _trend_decisions(profiles)
    journal = Journal(tmp_path)
    monkeypatch.setattr(Journal, "_append", staticmethod(_FailPlays(1)))
    with pytest.raises(OSError, match="disk full"):
        journal.record_decision(first)
    replayed = replace(first, as_of=first.as_of + MINUTE)
    journal.record_decision(replayed)
    assert [p["as_of"] for p in journal.plays()] == [
        first.as_of.isoformat(),
        replayed.as_of.isoformat(),
    ]


def test_a_written_play_record_is_not_written_again(
    profiles: Profiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, later = _trend_decisions(profiles)
    journal = Journal(tmp_path)
    journal.record_decision(first)
    journal.record_decision(later)
    journal.record_decision(first)  # (a replayed fixing decision is a new record)
    assert len(journal.plays()) == 2


# --- L2: the stand-in respects the decision time -------------------------------------


def test_the_stand_in_takes_no_entries_before_the_decision_time(profiles: Profiles) -> None:
    """The stand-in says its cause only where the real play caller could: in the prime
    session from the decision time on. Before it, BEFORE_DECISION; outside prime,
    OUTSIDE_PRIME; neither allows entries (review round 1, L2)."""
    from footprint.decision.play import PlayCaller
    from tests.agents.test_regime_reader import TODAY, _night_open

    inst = profiles.instrument
    due = PlayCaller(profiles.regime, profiles.play, profiles.strategy.data, inst).decision_time(
        TODAY
    )
    stand_in = FixedPlay(PlayCause.TREND)
    before = stand_in.call((), due - timedelta(seconds=1))
    assert before.cause is PlayCause.BEFORE_DECISION
    assert before.day == TODAY
    assert not before.entries_allowed
    at = stand_in.call((), due)
    assert at.cause is PlayCause.TREND
    assert at.day == TODAY
    assert not at.first  # a stand-in never fixes (journals) a day
    night = stand_in.call((), _night_open(TODAY, inst) + MINUTE)
    assert night.cause is PlayCause.OUTSIDE_PRIME
    assert night.day is None
    assert not night.entries_allowed
    assert stand_in.calls == [due - timedelta(seconds=1), due, _night_open(TODAY, inst) + MINUTE]
