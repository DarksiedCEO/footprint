"""An in-memory halt shared by execution and trade management.

The ledger's kill-switch latch is the durable record, but a disk write can fail at
exactly the wrong moment. The halt is set first, in memory, by anything that finds
the system in a state it cannot vouch for (a ledger that did not record a fill, a
reconcile mismatch, an exit that failed). While tripped, Diego sends no entry.
Only a restart (followed by reconcile and Andre clearing the latch) resets it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger("footprint.halt")


class Halt:
    def __init__(self) -> None:
        self._reasons: list[str] = []
        self._sink: Callable[[str], None] | None = None

    def route(self, sink: Callable[[str], None]) -> None:
        """Every future trip is also sent to `sink` (the runner's alarm to Andre).
        A later route replaces the earlier one."""
        self._sink = sink

    @property
    def tripped(self) -> bool:
        return bool(self._reasons)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(self._reasons)

    def trip(self, reason: str) -> None:
        if reason in self._reasons:
            return  # already halted for this: no repeat alarm
        log.critical("halt: %s", reason)
        self._reasons.append(reason)
        if self._sink is not None:
            try:
                self._sink(f"HALT: {reason}")
            except Exception:  # the halt itself must never fail
                log.exception("could not deliver a halt alarm")
