"""Core value objects passed between floors. All are immutable and self-validating."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, IntEnum
from types import MappingProxyType


def require_utc(ts: datetime, field: str) -> None:
    """Accept only datetimes whose tzinfo is UTC itself (not merely offset zero).

    The single UTC check for the whole codebase.
    """
    if not isinstance(ts, datetime):
        raise TypeError(f"{field} must be a datetime, got {type(ts).__name__}")
    if ts.tzinfo is not UTC:
        raise ValueError(f"{field} must be timezone-aware UTC (tzinfo=UTC), got {ts!r}")


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string, got {value!r}")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        _require_text(self.symbol, "Bar.symbol")
        require_utc(self.ts, "Bar.ts")
        for name in ("open", "high", "low", "close"):
            price = getattr(self, name)
            if not isinstance(price, Decimal):
                raise TypeError(f"Bar.{name} must be a Decimal, got {type(price).__name__}")
            if not price.is_finite() or price <= 0:
                raise ValueError(f"Bar.{name} must be a finite price > 0, got {price}")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise TypeError(f"Bar.volume must be an int, got {type(self.volume).__name__}")
        if self.volume < 0:
            raise ValueError(f"Bar.volume must be >= 0, got {self.volume}")
        if self.high < self.low:
            raise ValueError(f"Bar.high ({self.high}) must be >= Bar.low ({self.low})")
        if self.high < max(self.open, self.close):
            raise ValueError(f"Bar.high ({self.high}) must be >= open and close")
        if self.low > min(self.open, self.close):
            raise ValueError(f"Bar.low ({self.low}) must be <= open and close")


class Score(IntEnum):
    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


@dataclass(frozen=True, slots=True)
class Read:
    """One agent's opinion at a point in time.

    A Read is an opinion only. It must never carry anything that describes an
    order (side, size, price, stops); tests enforce the exact field set.
    """

    agent_id: str
    ts: datetime
    score: Score
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.agent_id, "Read.agent_id")
        require_utc(self.ts, "Read.ts")
        if not isinstance(self.score, Score):
            raise ValueError(f"Read.score must be a Score (-1, 0 or +1), got {self.score!r}")
        _require_text(self.reason, "Read.reason")


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class Signal:
    ts: datetime
    direction: Direction
    reads: tuple[Read, ...]

    def __post_init__(self) -> None:
        require_utc(self.ts, "Signal.ts")
        if not isinstance(self.direction, Direction):
            raise ValueError(f"Signal.direction must be a Direction, got {self.direction!r}")
        if not isinstance(self.reads, tuple):
            raise TypeError("Signal.reads must be a tuple of Read")
        if not self.reads:
            raise ValueError("Signal.reads must contain at least one contributing Read")
        seen: set[str] = set()
        for read in self.reads:
            if not isinstance(read, Read):
                raise TypeError(f"Signal.reads must contain only Read, got {type(read).__name__}")
            if read.ts > self.ts:
                raise ValueError(
                    f"Signal.reads must not look ahead: Read from {read.agent_id!r} at {read.ts} "
                    f"is later than Signal.ts {self.ts}"
                )
            if read.agent_id in seen:
                raise ValueError(f"Signal.reads has more than one Read from {read.agent_id!r}")
            seen.add(read.agent_id)


class TradeState(Enum):
    SCANNING = "SCANNING"
    SIGNAL = "SIGNAL"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


ALLOWED_TRANSITIONS: Mapping[TradeState, frozenset[TradeState]] = MappingProxyType(
    {
        TradeState.SCANNING: frozenset({TradeState.SIGNAL}),
        # SIGNAL -> CLOSED: vetoed or cancelled.
        TradeState.SIGNAL: frozenset({TradeState.ENTERING, TradeState.CLOSED}),
        # ENTERING -> CLOSED: unfilled or rejected.
        TradeState.ENTERING: frozenset({TradeState.OPEN, TradeState.CLOSED}),
        TradeState.OPEN: frozenset({TradeState.RUNNING, TradeState.EXITING}),
        TradeState.RUNNING: frozenset({TradeState.EXITING}),
        TradeState.EXITING: frozenset({TradeState.CLOSED}),
        TradeState.CLOSED: frozenset(),
    }
)


class IllegalTransitionError(ValueError):
    """A trade was asked to move between two states that are not connected."""


def _require_transition(current: TradeState, target: TradeState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal transition {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class TradeEvent:
    ts: datetime
    state: TradeState
    note: str = ""

    def __post_init__(self) -> None:
        require_utc(self.ts, "TradeEvent.ts")
        if not isinstance(self.state, TradeState):
            raise ValueError(f"TradeEvent.state must be a TradeState, got {self.state!r}")
        if not isinstance(self.note, str):
            raise TypeError("TradeEvent.note must be a string")


@dataclass(frozen=True, slots=True)
class Trade:
    """A trade's identity, current state and the ordered log of how it got there.

    The log is never empty and always opens with a SCANNING event. Create a trade
    with Trade.start() and move it with transition().
    """

    id: str
    state: TradeState
    events: tuple[TradeEvent, ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "Trade.id")
        if not isinstance(self.state, TradeState):
            raise ValueError(f"Trade.state must be a TradeState, got {self.state!r}")
        if not isinstance(self.events, tuple):
            raise TypeError("Trade.events must be a tuple of TradeEvent")
        for event in self.events:
            if not isinstance(event, TradeEvent):
                raise TypeError(
                    f"Trade.events must contain only TradeEvent, got {type(event).__name__}"
                )
        if not self.events:
            raise ValueError("Trade.events must not be empty; create a trade with Trade.start()")
        first = self.events[0].state
        if first is not TradeState.SCANNING:
            raise ValueError(f"Trade must start at {TradeState.SCANNING.value}, got {first.value}")
        for earlier, later in zip(self.events, self.events[1:], strict=False):
            if later.ts < earlier.ts:
                raise ValueError(
                    f"Trade.events must be in chronological order: {later.ts} follows {earlier.ts}"
                )
            _require_transition(earlier.state, later.state)
        if self.events[-1].state is not self.state:
            raise ValueError(
                f"Trade.state ({self.state.value}) must match the last event "
                f"({self.events[-1].state.value})"
            )

    @classmethod
    def start(cls, id: str, ts: datetime, note: str = "") -> Trade:
        """Open a new Trade whose log begins with a SCANNING event at `ts`."""
        return cls(
            id=id, state=TradeState.SCANNING, events=(TradeEvent(ts, TradeState.SCANNING, note),)
        )

    def transition(self, to: TradeState, ts: datetime, note: str = "") -> Trade:
        """Return a new Trade moved to `to`, with the move appended to the log."""
        if not isinstance(to, TradeState):
            raise TypeError(f"Trade.transition target must be a TradeState, got {to!r}")
        _require_transition(self.state, to)
        return Trade(id=self.id, state=to, events=(*self.events, TradeEvent(ts, to, note)))
