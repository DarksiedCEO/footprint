"""Stand-in play callers for tests that exercise everything AFTER the play caller.

Production code has no way to switch the play caller off: `Pipeline(profiles)` and the
backtest always build the real one (decision/play.py). Tests that check the crew, the
console, sizing, execution or exits on a short synthetic market (which the real regime
reader can never label: it needs ~61 sessions of history) hand the pipeline one of
these instead, so those tests keep testing what they were written for.

A stand-in keeps the real play caller's clock (review round 1, L2): it says its cause
only in the prime session from the decision time on (regime.decision_minutes after the
cash open, from the repo config). Before it, BEFORE_DECISION; outside prime,
OUTSIDE_PRIME. So no test passes on an entry production could never make. A stand-in
never fixes a day (`first` is never set): it journals no play record.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from functools import cache

from footprint.agents.regime_reader import Regime
from footprint.core.calendar import SessionKind, session_at, trading_day_at
from footprint.core.config import Profiles, load_profiles
from footprint.core.models import Bar
from footprint.decision.play import PlayCall, PlayCaller, PlayCause
from tests.conftest import REPO_ROOT

_REGIME = {
    PlayCause.TREND: Regime.TREND,
    PlayCause.CHOP: Regime.CHOP,
}


@cache
def _repo() -> tuple[Profiles, PlayCaller]:
    p = load_profiles(REPO_ROOT / "config", "nq.yaml")
    return p, PlayCaller(p.regime, p.play, p.strategy.data, p.instrument)


class FixedPlay:
    """Says `cause` at every call from the decision time on, and counts the calls."""

    def __init__(self, cause: PlayCause = PlayCause.TREND) -> None:
        self.cause = cause
        self.calls: list[datetime] = []

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> PlayCall:
        self.calls.append(as_of)
        profiles, clock = _repo()
        inst = profiles.instrument
        today = trading_day_at(as_of, inst)
        if today is None or session_at(as_of, inst) is not SessionKind.PRIME:
            return PlayCall(None, Regime.STAND_ASIDE, PlayCause.OUTSIDE_PRIME, "test stand-in")
        if as_of < clock.decision_time(today):
            return PlayCall(today, Regime.STAND_ASIDE, PlayCause.BEFORE_DECISION, "test stand-in")
        return PlayCall(
            day=today,
            regime=_REGIME.get(self.cause, Regime.STAND_ASIDE),
            cause=self.cause,
            reason=f"test stand-in: {self.cause.value}",
        )


class Switch:
    """TREND until `flip()`, then `then` (default STAND_ASIDE by the rules)."""

    def __init__(self, then: PlayCause = PlayCause.RULES) -> None:
        self.cause = PlayCause.TREND
        self.then = then

    def flip(self) -> None:
        self.cause = self.then

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> PlayCall:
        return FixedPlay(self.cause).call(minute_bars, as_of)


def open_play() -> FixedPlay:
    """The play caller as it would be on a TREND day: entries as before the wiring."""
    return FixedPlay(PlayCause.TREND)
