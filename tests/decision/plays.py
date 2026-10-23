"""Stand-in play callers for tests that exercise everything AFTER the play caller.

Production code has no way to switch the play caller off: `Pipeline(profiles)` and the
backtest always build the real one (decision/play.py). Tests that check the crew, the
console, sizing, execution or exits on a short synthetic market (which the real regime
reader can never label: it needs ~61 sessions of history) hand the pipeline one of
these instead, so those tests keep testing what they were written for.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from footprint.agents.regime_reader import Regime
from footprint.core.models import Bar
from footprint.decision.play import PlayCall, PlayCause

_REGIME = {
    PlayCause.TREND: Regime.TREND,
    PlayCause.CHOP: Regime.CHOP,
}


class FixedPlay:
    """Says `cause` at every call, and counts the calls."""

    def __init__(self, cause: PlayCause = PlayCause.TREND) -> None:
        self.cause = cause
        self.calls: list[datetime] = []

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> PlayCall:
        self.calls.append(as_of)
        return PlayCall(
            day=as_of.date(),
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
