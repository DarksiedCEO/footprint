"""Named exit rules for the backtest only (Q5 rule library). See docs/strategy-rules.md.

Each rule answers one question for trade management, asked last on a fresh quote after
every existing reason to get out (the hard stop first): does this rule want the whole
position out now? It can only add a way out; the exit itself is the existing one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from footprint.execution.executor.diego import Position


class TargetExit:
    """Q5.5 "target", APPROVED BY ANDRE 2026-10-10: when the position's open profit
    reaches the configured profit target, close ALL contracts; no trail, no scale-out,
    no runner; the hard loss stop unchanged.

    Readings Q5.5-a (APPROVED BY ANDRE 2026-10-11 10:30 PT): the open profit is trade
    management's figure, on the bid and net of round-trip fees (the basis the trail and
    every risk check use); "reaches" is at or above. Because trade management asks the
    rule before it moves the trail, the trade is out on the first fresh quote at the
    target, before the trail can engage.

    Q5.5-b LATCHED (APPROVED BY ANDRE 2026-10-11 10:30 PT): once the open profit reaches
    the target, the verdict is held for that position until it is gone, as the "time"
    rule holds an "out" (Q5.6-a). An exit that cannot begin yet (positions uncertain, a
    refused stop cancel) waits exactly as before; it then closes as soon as an exit can
    begin, at the bid then, even if the bid has fallen back below the target. Verdicts
    are per position; a new position starts unlatched. The exit itself (stop cancelled
    first, limit at the bid, never sold twice) and the hard stop, asked first, are
    unchanged."""

    __slots__ = ("_latched", "target")

    def __init__(self, target: Decimal) -> None:
        self.target = target
        self._latched: set[str] = set()

    def wants_out(self, position: Position, net_open: Decimal, now: datetime) -> bool:
        pid = position.position_id
        if pid in self._latched:
            return True
        if net_open >= self.target:
            self._latched.add(pid)
            return True
        return False
