"""Named entry rules for the backtest only (Q5 rule library).

A rule is a factory: Profiles in, an EntryTrigger out. The trigger replaces only the
momentum read inside the decision pipeline; every other gate (trend agreement,
session, risk, kill switch, levels, sizing, the hard stop) still decides. Nothing
outside footprint.backtest imports this module, so no rule can reach the live path.
With no rule named the backtest builds the pipeline exactly as the live runner does.
"""

from __future__ import annotations

from collections.abc import Callable

from footprint.core.config import Profiles
from footprint.decision.pipeline import EntryTrigger

EntryRule = Callable[[Profiles], EntryTrigger]

ENTRY_RULES: dict[str, EntryRule] = {}
