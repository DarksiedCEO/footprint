"""Named entry and exit rules for the backtest only (Q5 rule library).

An entry rule is a factory: Profiles and the rule library's numbers (config/backtest.yaml
`rules:`) in, an EntryTrigger out. The trigger replaces
only the momentum read inside the decision pipeline; every other gate (trend
agreement, session, risk, kill switch, levels, sizing, the hard stop) still decides.

An exit rule is a factory: Profiles in, an ExitRule out. Trade management asks it
last, on a fresh quote, after every existing reason to get out (hard stop and gap
first); it can only add a way out and never moves or loosens the hard stop.

Nothing outside footprint.backtest imports this module, so no rule can reach the live
path. With no rule named the backtest builds the pipeline and trade management
exactly as the live runner does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from footprint.agents.momentum_reader import MomentumReader
from footprint.backtest.settings import RuleSettings
from footprint.backtest.stairstep import StairTrigger
from footprint.core.config import Profiles
from footprint.decision.pipeline import EntryTrigger
from footprint.execution.management.malik import ExitRule

EntryRule = Callable[[Profiles, RuleSettings], EntryTrigger]
ExitRuleFactory = Callable[[Profiles], ExitRule]


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


ENTRY_RULES: dict[str, EntryRule] = {
    "breakout": breakout,
    "stairstep": stairstep,
    "stairstep_vol": stairstep_vol,
}
EXIT_RULES: dict[str, ExitRuleFactory] = {}
