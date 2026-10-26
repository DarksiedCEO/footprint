"""Option quotes and chains: what Felix chooses from and what Diego prices against.

Prices are option premiums in index points. All objects are immutable and
self-validating; a malformed quote can never reach sizing or execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from footprint.core.models import Direction, require_utc


class Right(Enum):
    CALL = "CALL"
    PUT = "PUT"

    @staticmethod
    def for_direction(direction: Direction) -> Right:
        """Up = calls, down = puts. v1 only ever buys options."""
        return Right.CALL if direction is Direction.LONG else Right.PUT


def _require_price(value: Decimal, field: str, *, allow_zero: bool) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{field} must be a finite price {'>=' if allow_zero else '>'} 0")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """Top of book for one option contract. A bid of 0 means no bid."""

    contract_id: str
    expiry: date
    strike: Decimal
    right: Right
    bid: Decimal
    ask: Decimal
    open_interest: int

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("OptionQuote.contract_id must be a non-empty string")
        if not isinstance(self.expiry, date) or isinstance(self.expiry, datetime):
            raise TypeError("OptionQuote.expiry must be a date")
        _require_price(self.strike, "OptionQuote.strike", allow_zero=False)
        if not isinstance(self.right, Right):
            raise TypeError("OptionQuote.right must be a Right")
        _require_price(self.bid, "OptionQuote.bid", allow_zero=True)
        _require_price(self.ask, "OptionQuote.ask", allow_zero=False)
        if self.ask < self.bid:
            raise ValueError(f"OptionQuote {self.contract_id}: ask {self.ask} < bid {self.bid}")
        if isinstance(self.open_interest, bool) or not isinstance(self.open_interest, int):
            raise TypeError("OptionQuote.open_interest must be an int")
        if self.open_interest < 0:
            raise ValueError("OptionQuote.open_interest must be >= 0")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class OptionChain:
    """Every quote available at `ts` for one underlying, plus the underlying's price."""

    underlying: str
    ts: datetime
    underlying_price: Decimal
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            raise ValueError("OptionChain.underlying must be a non-empty string")
        require_utc(self.ts, "OptionChain.ts")
        _require_price(self.underlying_price, "OptionChain.underlying_price", allow_zero=False)
        if not isinstance(self.quotes, tuple):
            raise TypeError("OptionChain.quotes must be a tuple of OptionQuote")
        seen: set[str] = set()
        for quote in self.quotes:
            if not isinstance(quote, OptionQuote):
                raise TypeError("OptionChain.quotes must contain only OptionQuote")
            if quote.contract_id in seen:
                raise ValueError(f"OptionChain has duplicate contract {quote.contract_id!r}")
            seen.add(quote.contract_id)


_GREEK_FIELDS = ("implied_vol", "delta", "gamma", "vega", "theta", "underlying_price")


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    """One option's complete model Greeks (IBKR's model computation), stamped when they
    arrived. Every value is present: a reading with any value missing is not usable
    (see `checked_greeks`).

    A reading impossible for an option can never be built: an implied volatility at or
    below 0, a call delta outside 0..1 or a put delta outside -1..0, a negative gamma or
    vega, an underlying price at or below 0, or any value that is not finite. Theta has
    no sign rule (a deep in-the-money European option can gain value with time)."""

    right: Right
    implied_vol: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    underlying_price: Decimal
    ts: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.right, Right):
            raise TypeError("OptionGreeks.right must be a Right")
        require_utc(self.ts, "OptionGreeks.ts")
        for name in _GREEK_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"OptionGreeks.{name} must be a Decimal")
            if not value.is_finite():
                raise ValueError(f"OptionGreeks.{name} must be finite, got {value}")
        low, high = (
            (Decimal(0), Decimal(1)) if self.right is Right.CALL else (-Decimal(1), Decimal(0))
        )
        if not low <= self.delta <= high:
            raise ValueError(f"OptionGreeks.delta {self.delta} is impossible for a {self.right}")
        if self.implied_vol <= 0:
            raise ValueError(f"OptionGreeks.implied_vol must be > 0, got {self.implied_vol}")
        for name in ("gamma", "vega"):
            if getattr(self, name) < 0:
                raise ValueError(f"OptionGreeks.{name} must be >= 0, got {getattr(self, name)}")
        if self.underlying_price <= 0:
            raise ValueError(
                f"OptionGreeks.underlying_price must be > 0, got {self.underlying_price}"
            )


class GreeksVerdict(Enum):
    """What `judge_greeks` made of one reading."""

    USABLE = "USABLE"  # complete and possible: an OptionGreeks
    NOT_COMPUTED = "NOT_COMPUTED"  # IBKR computed nothing (yet)
    INCOMPLETE = "INCOMPLETE"  # some values computed, some not: not usable
    IMPOSSIBLE = "IMPOSSIBLE"  # complete, but impossible for an option


def judge_greeks(
    right: Right,
    ts: datetime,
    *,
    implied_vol: Decimal | None,
    delta: Decimal | None,
    gamma: Decimal | None,
    vega: Decimal | None,
    theta: Decimal | None,
    underlying_price: Decimal | None,
) -> tuple[GreeksVerdict, OptionGreeks | None]:
    """The verdict on a reading, with its OptionGreeks exactly when USABLE. Only a usable
    reading is ever returned: one value missing makes the whole reading unusable. A value
    of the wrong type, or a time that is not UTC, is a caller's bug and raises."""
    require_utc(ts, "judge_greeks ts")  # a bad time is a caller's bug, never a verdict
    values = (implied_vol, delta, gamma, vega, theta, underlying_price)
    for name, value in zip(_GREEK_FIELDS, values, strict=True):
        if value is not None and not isinstance(value, Decimal):
            raise TypeError(f"judge_greeks {name} must be a Decimal or None")
    if all(v is None for v in values):
        return GreeksVerdict.NOT_COMPUTED, None
    if (
        implied_vol is None
        or delta is None
        or gamma is None
        or vega is None
        or theta is None
        or underlying_price is None
    ):
        return GreeksVerdict.INCOMPLETE, None
    try:
        greeks = OptionGreeks(right, implied_vol, delta, gamma, vega, theta, underlying_price, ts)
    except ValueError:
        return GreeksVerdict.IMPOSSIBLE, None
    return GreeksVerdict.USABLE, greeks


def checked_greeks(
    right: Right,
    ts: datetime,
    *,
    implied_vol: Decimal | None,
    delta: Decimal | None,
    gamma: Decimal | None,
    vega: Decimal | None,
    theta: Decimal | None,
    underlying_price: Decimal | None,
) -> OptionGreeks | None:
    """The reading as OptionGreeks when usable (complete and possible), else None."""
    _, greeks = judge_greeks(
        right,
        ts,
        implied_vol=implied_vol,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        underlying_price=underlying_price,
    )
    return greeks
