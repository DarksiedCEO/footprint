"""Booker (stop): the hard stop and the trail. The stop never moves away from entry.

v1 always holds long options, so a stop is a premium level BELOW the entry fill,
triggered when the option's BID trades at or through it (G2: you can only sell at
the bid).

Booker OWNS every live stop: positions are keyed by id, and the only way to change
a stop is through Booker. Callers get immutable copies; altering a copy changes
nothing Booker uses, so it cannot reach the broker (NB-01).

- Hard stop: entry fill minus stop_loss of option value across the position,
  rounded UP to the option tick, so the loss never exceeds stop_loss (fees aside).
- Trail (G7, net of fees per G15): once the gain on the bid, after round-trip fees,
  reaches profit_target, the stop moves so that trail_keep_pct of the peak net
  gain is locked, and then follows the peak at that share. No volume or theta
  adjustment until those agents exist.
- Every change goes through the ratchet: a request that would move the stop
  further from entry, or off the tick, is refused, logged, and changes nothing.
- A tick's `ts` is when the feed last CONFIRMED that bid (a snapshot/heartbeat
  time), not when the price last changed. Stale ticks never move a stop and are
  reported as STALE; a vanished bid is reported as NO_BID (treat as a gap). Only
  CLEAR from `check` means nothing needs doing.
- The peak (and so the trail) only advances to a level two consecutive bids agree
  on; the stop itself triggers on a single bid.
- Resting order (G1): a stop-limit at the stop with its limit limit_offset_ticks
  below. If the bid falls below that limit, trade management must flatten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from footprint.core.config import (
    ExecutionProfile,
    InstrumentProfile,
    RiskProfile,
    check_consistency,
)
from footprint.core.models import require_utc
from footprint.decision.sizing import hard_stop_price, round_to_tick, stop_fits

log = logging.getLogger("footprint.stop")


@dataclass(frozen=True, slots=True)
class StopLevel:
    """A read-only copy of one position's stop."""

    position_id: str
    entry: Decimal
    contracts: int
    price: Decimal
    peak_bid: Decimal
    trailing: bool


@dataclass(frozen=True, slots=True)
class BidTick:
    """The option's best bid at `ts`. A bid of 0 means there is no bid."""

    bid: Decimal
    ts: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.bid, Decimal) or not self.bid.is_finite() or self.bid < 0:
            raise ValueError(f"BidTick.bid must be a finite Decimal >= 0, got {self.bid!r}")
        require_utc(self.ts, "BidTick.ts")


class TickStatus(Enum):
    USED = "USED"
    STALE = "STALE"
    NO_BID = "NO_BID"


class StopCheck(Enum):
    """What one bid says about a stop. Only CLEAR means "nothing to do"."""

    CLEAR = "CLEAR"  # bid above the stop
    HIT = "HIT"  # bid at or below the stop: exit
    GAPPED = "GAPPED"  # bid below the resting order's limit: it may never fill; flatten
    STALE = "STALE"  # no fresh bid: the stop cannot be judged; escalate
    NO_BID = "NO_BID"  # the bid has vanished: treat as a gap; flatten and escalate


@dataclass(frozen=True, slots=True)
class Refusal:
    position_id: str
    requested: Decimal
    kept: Decimal
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class StopOrderSpec:
    trigger: Decimal
    limit: Decimal


class StopKeeper:
    def __init__(
        self, risk: RiskProfile, instrument: InstrumentProfile, execution: ExecutionProfile
    ) -> None:
        check_consistency(instrument, risk, execution)
        self._risk = risk
        self._instrument = instrument
        self._execution = execution
        self._stops: dict[str, StopLevel] = {}
        self._last_bid: dict[str, Decimal] = {}
        self._closed: set[str] = set()

    @property
    def agent_id(self) -> str:
        return "stop"

    # --- lifecycle ----------------------------------------------------------------

    def open(self, position_id: str, fill: Decimal, contracts: int) -> StopLevel:
        """Set the hard stop for a new position. Once per position id."""
        if not isinstance(position_id, str) or not position_id.strip():
            raise ValueError("position_id must be a non-empty string")
        if position_id in self._stops or position_id in self._closed:
            raise ValueError(f"position {position_id!r} already has (or had) a stop")
        if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
            raise ValueError("contracts must be an int >= 1")
        if contracts > self._risk.max_contracts:
            raise ValueError(f"contracts {contracts} > max_contracts {self._risk.max_contracts}")
        tick = self._instrument.option_tick_size
        if not isinstance(fill, Decimal) or not fill.is_finite() or fill <= 0:
            raise ValueError(f"fill must be a finite Decimal > 0, got {fill!r}")
        if fill % tick:
            raise ValueError(f"fill {fill} is not on the option tick {tick}")
        if not stop_fits(fill, contracts, self._risk, self._instrument, self._execution):
            raise ValueError(f"fill {fill} is too small to carry the hard stop and its limit")
        level = StopLevel(
            position_id=position_id,
            entry=fill,
            contracts=contracts,
            price=hard_stop_price(fill, contracts, self._risk, self._instrument),
            peak_bid=fill,
            trailing=False,
        )
        self._stops[position_id] = level
        self._last_bid[position_id] = fill
        return replace(level)

    def current(self, position_id: str) -> StopLevel:
        return replace(self._stops[position_id])

    def close(self, position_id: str) -> StopLevel:
        """Retire a position's stop once the position is flat. The id can never be
        reused, so closing and reopening cannot widen a stop.
        """
        level = self._stops.pop(position_id)
        self._last_bid.pop(position_id, None)
        self._closed.add(position_id)
        log.info("stop retired for %s at %s", position_id, level.price)
        return level

    # --- changes --------------------------------------------------------------------

    def request(self, position_id: str, price: Decimal, source: str) -> Refusal | None:
        """Move the stop to `price` if that is no further from entry and on the tick."""
        stop = self._stops[position_id]
        tick = self._instrument.option_tick_size
        reason = ""
        if not isinstance(price, Decimal) or not price.is_finite():
            reason = "not a finite price"
        elif price % tick:
            reason = f"off the option tick {tick}"
        elif price < stop.price:
            reason = "would widen the stop"
        if reason:
            log.warning(
                "stop change refused for %s: %s asked %s (%s); stop stays %s",
                position_id,
                source,
                price,
                reason,
                stop.price,
            )
            return Refusal(position_id, price, stop.price, source, reason)
        self._stops[position_id] = replace(stop, price=price)
        return None

    def on_tick(self, position_id: str, tick: BidTick, as_of: datetime) -> TickStatus:
        """Track the peak bid and move the trail. Never lowers the stop."""
        status = self._usable(tick, as_of)
        if status is not TickStatus.USED:
            return status
        stop = self._stops[position_id]
        # The peak only advances to a level two consecutive bids agree on, so one bad
        # print cannot drag the trail up and stop out a healthy position.
        confirmed = min(tick.bid, self._last_bid[position_id])
        self._last_bid[position_id] = tick.bid
        stop = replace(stop, peak_bid=max(stop.peak_bid, confirmed))
        self._stops[position_id] = stop
        pv = self._instrument.point_value
        fees = 2 * self._risk.fee_per_contract_per_side * stop.contracts
        net_peak = (stop.peak_bid - stop.entry) * pv * stop.contracts - fees
        if net_peak < self._risk.profit_target:
            return status
        keep = net_peak * self._risk.trail_keep_pct.scaleb(-2)
        target = round_to_tick(
            stop.entry + (keep + fees) / (pv * stop.contracts),
            self._instrument.option_tick_size,
            up=True,
        )
        if target > stop.price:
            self.request(position_id, target, "trail")
        self._stops[position_id] = replace(self._stops[position_id], trailing=True)
        return status

    # --- questions ------------------------------------------------------------------

    def check(self, position_id: str, tick: BidTick, as_of: datetime) -> StopCheck:
        """Judge the stop on one bid. A single bid is enough to exit (the protective side
        errs toward getting out); anything but CLEAR needs action from the caller.
        """
        status = self._usable(tick, as_of)
        if status is TickStatus.STALE:
            return StopCheck.STALE
        if status is TickStatus.NO_BID:
            return StopCheck.NO_BID
        if tick.bid < self.resting_order(position_id).limit:
            return StopCheck.GAPPED
        if tick.bid <= self._stops[position_id].price:
            return StopCheck.HIT
        return StopCheck.CLEAR

    def resting_order(self, position_id: str) -> StopOrderSpec:
        stop = self._stops[position_id]
        tick = self._instrument.option_tick_size
        limit = stop.price - tick * self._execution.stop_order.limit_offset_ticks
        return StopOrderSpec(trigger=stop.price, limit=max(limit, tick))

    def _usable(self, tick: BidTick, as_of: datetime) -> TickStatus:
        require_utc(as_of, "as_of")
        age = as_of - tick.ts
        if age < timedelta(0) or age > timedelta(seconds=self._execution.quotes.max_age_seconds):
            return TickStatus.STALE
        if tick.bid == 0:
            return TickStatus.NO_BID
        return TickStatus.USED
