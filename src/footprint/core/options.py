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
