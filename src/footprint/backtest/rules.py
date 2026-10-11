"""Named entry and exit rules for the backtest only (Q5 rule library).

An entry rule is a factory: Profiles and the rule library's numbers (config/backtest.yaml
`rules:`) in, an EntryTrigger out. The trigger replaces
only the momentum read inside the decision pipeline; every other gate (trend
agreement, session, risk, kill switch, levels, sizing, the hard stop) still decides.

An exit rule is a factory: Profiles in, an ExitRule (or None) out. Trade management
asks the ExitRule last, on a fresh quote, after every existing reason to get out (hard
stop and gap first); it can only add a way out and never moves or loosens the hard
stop. None adds no way out: trade management is built exactly as the live runner
builds it (the baseline "trail").

Nothing outside footprint.backtest imports this module, so no rule can reach the live
path. With no rule named the backtest builds the pipeline and trade management
exactly as the live runner does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from datetime import timedelta

from footprint.agents.momentum_reader import MomentumReader
from footprint.backtest.pullback import PullbackSettings, PullbackTrigger
from footprint.backtest.settings import RuleSettings
from footprint.backtest.stairstep import StairSettings, StairTrigger
from footprint.core.config import Profiles
from footprint.decision.pipeline import EntryTrigger
from footprint.execution.management.malik import ExitRule

EntryRule = Callable[[Profiles, RuleSettings], EntryTrigger]
ExitRuleFactory = Callable[[Profiles], ExitRule | None]


_ENTRY_BARS = timedelta(minutes=1)  # the pipeline's entry timeframe: completed 1-min bars


def breakout(profiles: Profiles, rule_settings: RuleSettings) -> EntryTrigger:
    """Q5.1 "breakout", the BASELINE: the existing momentum entry, unchanged (Darius,
    built exactly as the pipeline builds it with no rule named). See
    docs/strategy-rules.md."""
    return MomentumReader(profiles.strategy.volume, profiles.instrument)


def stairstep(profiles: Profiles, rule_settings: RuleSettings) -> EntryTrigger:
    """Q5.2 "stairstep": the configured count of consecutive valid steps, entry on the
    first close above the last push's high (no volume condition). See
    docs/strategy-rules.md."""
    return StairTrigger(rule_settings.stairstep, _ENTRY_BARS, with_volume=False)


def stairstep_vol(profiles: Profiles, rule_settings: RuleSettings) -> EntryTrigger:
    """Q5.2 "stairstep_vol": "stairstep" plus, for each push, average bar volume above
    that of the pause that follows it. See docs/strategy-rules.md."""
    return StairTrigger(rule_settings.stairstep, _ENTRY_BARS, with_volume=True)


def pullback(profiles: Profiles, rule_settings: RuleSettings) -> EntryTrigger:
    """Q5.3 "pullback": after a volume spike in the trade direction (the spike test
    "breakout" uses, with the same live numbers), a dip to the spike candle's high that
    holds and turns back up, within the configured lifetime. See
    docs/strategy-rules.md."""
    return PullbackTrigger(
        rule_settings.pullback,
        profiles.strategy.volume,
        profiles.instrument.prime_session.timezone,
        _ENTRY_BARS,
    )


ENTRY_RULES: dict[str, EntryRule] = {
    "breakout": breakout,
    "pullback": pullback,
    "stairstep": stairstep,
    "stairstep_vol": stairstep_vol,
}
_STAIR_RULES = frozenset({"stairstep", "stairstep_vol"})


def entry_rule_numbers(name: str, rule_settings: RuleSettings) -> dict[str, str]:
    """The rule-library numbers the named entry rule runs with, for the readout's
    assumptions ("rule <section> <key>" -> value), so a run names the numbers that
    produced it (reviews G47-1, G48-3). "breakout" reads none of them."""
    if name in _STAIR_RULES:
        stair = rule_settings.stairstep
        return {
            f"rule stairstep {f.name}": str(getattr(stair, f.name)) for f in fields(StairSettings)
        }
    if name == "pullback":
        life = rule_settings.pullback
        return {
            f"rule pullback {f.name}": str(getattr(life, f.name)) for f in fields(PullbackSettings)
        }
    return {}


def trail(profiles: Profiles) -> ExitRule | None:
    """Q5.4 "trail", the BASELINE: the existing exit, unchanged (the hard loss stop and
    every existing reason to get out; from the profit target, net of fees, the stop
    locks a fixed share of the peak net gain and follows it). That trail has no volume
    adjustment, and scale-out is not built (one contract), whatever the approved wording
    says: see reading Q5.4-b. That exit is trade management itself, so this rule adds
    nothing: None, and the backtest builds trade management exactly as with no rule
    named. See docs/strategy-rules.md."""
    return None


EXIT_RULES: dict[str, ExitRuleFactory] = {
    "trail": trail,
}
