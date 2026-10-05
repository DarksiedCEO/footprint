"""Booker (stop): the hard stop and the trail. The stop never moves away from entry.

v1 always holds long options, so a stop is a premium level BELOW the entry fill,
triggered when the option's BID trades at or through it (G2: you can only sell at
the bid).

- Hard stop: entry fill minus stop_loss of option value across the position,
  rounded UP to the option tick, so the loss never exceeds stop_loss (fees aside).
- Trail (G7): once the gain on the bid reaches profit_target, the stop jumps to
  entry + trail_keep_pct of the peak gain and then follows the peak at that share.
  No volume or theta adjustment until those agents exist.
- Ratchet (NB-01): every change goes through `ratchet`; a request that would move
  the stop further from entry is refused, logged, and leaves the stop unchanged.
- Resting order (G1): the broker holds a stop-limit at the stop with its limit
  limit_offset_ticks below. If the bid gaps through that limit, trade management
  must flatten (see `gapped_through`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal

from footprint.core.config import ExecutionProfile, InstrumentProfile, RiskProfile
from footprint.decision.sizing import round_to_tick

log = logging.getLogger("footprint.stop")


@dataclass(frozen=True, slots=True)
class StopLevel:
    entry: Decimal
    contracts: int
    price: Decimal
    peak_bid: Decimal
    trailing: bool

    def __post_init__(self) -> None:
        for name in ("entry", "price", "peak_bid"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"StopLevel.{name} must be a finite Decimal > 0")
        if isinstance(self.contracts, bool) or not isinstance(self.contracts, int):
            raise TypeError("StopLevel.contracts must be an int")
        if self.contracts < 1:
            raise ValueError("StopLevel.contracts must be >= 1")


@dataclass(frozen=True, slots=True)
class Refusal:
    requested: Decimal
    kept: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class StopOrderSpec:
    trigger: Decimal
    limit: Decimal


class StopKeeper:
    def __init__(
        self, risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
    ) -> None:
        self._risk = risk
        self._instrument = instrument
        self._execution = execution

    @property
    def agent_id(self) -> str:
        return "stop"

    def _points(self, dollars: Decimal, contracts: int) -> Decimal:
        return dollars / (self._instrument.point_value * contracts)

    def initial(self, entry: Decimal, contracts: int) -> StopLevel:
        """The hard stop for a fresh fill of `contracts` at `entry`."""
        tick = self._instrument.option_tick_size
        price = round_to_tick(entry - self._points(self._risk.stop_loss, contracts), tick, up=True)
        if price < tick:
            raise ValueError(f"entry {entry} is too small to carry the hard stop")
        return StopLevel(
            entry=entry, contracts=contracts, price=price, peak_bid=entry, trailing=False
        )

    def ratchet(
        self, stop: StopLevel, requested: Decimal, source: str
    ) -> tuple[StopLevel, Refusal | None]:
        """Move the stop to `requested` only if that is no further from entry."""
        if requested < stop.price:
            refusal = Refusal(requested=requested, kept=stop.price, source=source)
            log.warning(
                "stop widening refused: %s asked %s, stop stays %s", source, requested, stop.price
            )
            return stop, refusal
        return replace(stop, price=requested), None

    def on_bid(self, stop: StopLevel, bid: Decimal) -> StopLevel:
        """Update the peak and the trail for a new bid. Never lowers the stop."""
        peak = max(stop.peak_bid, bid)
        stop = replace(stop, peak_bid=peak)
        peak_gain = (peak - stop.entry) * self._instrument.point_value * stop.contracts
        if peak_gain < self._risk.profit_target:
            return stop
        keep = peak_gain * self._risk.trail_keep_pct.scaleb(-2)
        target = round_to_tick(
            stop.entry + self._points(keep, stop.contracts),
            self._instrument.option_tick_size,
            up=False,
        )
        moved, _ = self.ratchet(stop, max(target, stop.price), "trail")
        return replace(moved, trailing=True)

    @staticmethod
    def hit(stop: StopLevel, bid: Decimal) -> bool:
        return bid <= stop.price

    def resting_order(self, stop: StopLevel) -> StopOrderSpec:
        tick = self._instrument.option_tick_size
        limit = stop.price - tick * self._execution.stop_order.limit_offset_ticks
        return StopOrderSpec(trigger=stop.price, limit=max(limit, tick))

    def gapped_through(self, stop: StopLevel, bid: Decimal) -> bool:
        """The bid is below the resting order's limit: it may never fill; flatten now."""
        return bid < self.resting_order(stop).limit
