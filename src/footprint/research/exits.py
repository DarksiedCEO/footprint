"""Andre's exits, in OPTION dollars, judged on each modelled option value of a trade.

All amounts are per contract. The basis follows the trading system (decision/stop.py):
- the stop is GROSS option value: out when the value has fallen `stop_dollars` below the
  entry value (risk.stop_loss);
- open profit for the lock and the scale-out target is NET of the round-trip cost per
  contract (D13 / G15: every figure the risk layer sees includes costs).

Kinds:
- `booker` (`booker_contracts`): the stop; and once the peak net open profit reaches
  `lock_start_dollars` (risk.profit_target), `lock_fraction` (risk.trail_keep_pct) of
  that peak is locked: out when net open profit falls to the floor. The floor follows
  the peak up and never falls.
- `scale_out` (`scale_out_contracts`, more than one): one contract is sold when net
  open profit reaches `first_target_dollars` (risk.profit_target); the rest ride the
  booker's stop and lock. Before the target every contract is on the stop.

The caller hands the exit one price point at a time, in the order the minute is assumed
to trade (research/engine.py: open, then the extreme AGAINST the trade, then the one for
it, then the close; adverse first, as the backtest path model). A level crossed inside
a continuous move fills AT the level; a level the price opened through (`gap`) fills at
the price. A rule may add a level of its own (`futures_level`: the option value at the
rule's futures stop); whichever level is highest is reached first by a falling value.

Nothing is ever sold twice: a closed contract is gone, and every fill reduces `open`.
"""

from __future__ import annotations

from dataclasses import dataclass

from footprint.research.settings import ExitSettings

KINDS = ("booker", "scale_out")


@dataclass(frozen=True, slots=True)
class Fill:
    contracts: int
    value: float  # option value (points) the contracts were sold at
    reason: str  # stop, lock, futures_stop, target, or the caller's flat reason


class OptionExit:
    def __init__(
        self, kind: str, entry_value: float, ex: ExitSettings, *, point_value: float, cost: float
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown exit kind {kind!r}; known: {KINDS}")
        self.kind = kind
        self.entry_value = entry_value
        self._ex = ex
        self._pv = point_value
        self._cost = cost
        self.contracts = ex.booker_contracts if kind == "booker" else ex.scale_out_contracts
        self.open = self.contracts
        self._target_taken = kind == "booker"
        self._peak_net: float | None = None

    @property
    def done(self) -> bool:
        return self.open == 0

    def _net(self, value: float) -> float:
        return (value - self.entry_value) * self._pv - self._cost

    def _value_at_net(self, net: float) -> float:
        return self.entry_value + (net + self._cost) / self._pv

    def _floor(self) -> float | None:
        if self._peak_net is None or self._peak_net < self._ex.lock_start_dollars:
            return None
        return self._value_at_net(self._peak_net * self._ex.lock_fraction)

    def check(self, value: float, *, gap: bool, futures_level: float | None = None) -> list[Fill]:
        """Fills caused by the option trading at `value` (in path order)."""
        if self.done:
            return []
        net = self._net(value)
        if self._peak_net is None or net > self._peak_net:
            self._peak_net = net
        levels: list[tuple[float, str]] = [
            (self.entry_value - self._ex.stop_dollars / self._pv, "stop")
        ]
        floor = self._floor()
        if floor is not None:
            levels.append((floor, "lock"))
        if futures_level is not None:
            levels.append((futures_level, "futures_stop"))
        level, reason = levels[0]
        for candidate in levels[1:]:
            if candidate[0] > level:
                level, reason = candidate
        if value <= level:
            sold = Fill(self.open, value if gap else level, reason)
            self.open = 0
            return [sold]
        if not self._target_taken:
            target = self._value_at_net(self._ex.first_target_dollars)
            if value >= target:
                self._target_taken = True
                self.open -= 1
                return [Fill(1, value if gap else target, "target")]
        return []

    def flat(self, value: float, reason: str) -> list[Fill]:
        """Sell whatever is still open at `value` (the rule's hard flat, end of data)."""
        if self.done:
            return []
        sold = Fill(self.open, value, reason)
        self.open = 0
        return [sold]

    def net(self, fills: list[Fill]) -> float:
        """Net dollars of these fills (each contract's round trip cost included)."""
        return sum(
            (f.value - self.entry_value) * self._pv * f.contracts - self._cost * f.contracts
            for f in fills
        )
