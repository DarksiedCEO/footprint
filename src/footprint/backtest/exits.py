"""Named exit rules for the backtest only (Q5 rule library). See docs/strategy-rules.md.

Each rule answers one question for trade management, asked last on a fresh quote after
every existing reason to get out (the hard stop first): does this rule want the whole
position out now? It can only add a way out; the exit itself is the existing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from footprint.execution.executor.diego import Position


@dataclass(frozen=True, slots=True)
class TargetExit:
    """Q5.5 "target", APPROVED BY ANDRE 2026-10-10: when the position's open profit
    reaches the configured profit target, close ALL contracts; no trail, no scale-out,
    no runner; the hard loss stop unchanged.

    Readings Q5.5-a (for Andre): the open profit is trade management's figure, on the
    bid and net of round-trip fees (the basis the trail and every risk check use);
    "reaches" is at or above. Because trade management asks the rule before it moves
    the trail, the trade is out on the first fresh quote at the target, before the
    trail can engage."""

    target: Decimal

    def wants_out(self, position: Position, net_open: Decimal, now: datetime) -> bool:
        return net_open >= self.target
