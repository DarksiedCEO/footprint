"""Named entry and exit rules for the backtest only (Q5 rule library).

An entry rule is a factory: Profiles in, an EntryTrigger out. The trigger replaces
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

from footprint.core.config import Profiles
from footprint.decision.pipeline import EntryTrigger
from footprint.execution.management.malik import ExitRule

EntryRule = Callable[[Profiles], EntryTrigger]
ExitRuleFactory = Callable[[Profiles], ExitRule]

ENTRY_RULES: dict[str, EntryRule] = {}
EXIT_RULES: dict[str, ExitRuleFactory] = {}
