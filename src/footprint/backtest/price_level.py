"""Price-level rescaling for the backtest only (backtest-history-v1, decision BH-2).

The option model (options_model.py, G4) prices the at-the-money option at a fixed
`atm_premium_points` x root(days to expiry), whatever the futures price: it means
something only near the level it was set for (GV-3, GV-4). Over a long history the
futures price moves through many levels, so with `option_model.reference_price` set,
the backtest models every option as if the future traded at that level, as the
research harness does (footprint.research.pricing):

- each trade's futures moves are rescaled: the model is quoted at
  F x reference / anchor, where F is the modelled futures price and the anchor is the
  futures price at the last moment nothing was live (no working entry, no position,
  no reconcile running): the price the trade's entry was decided at. It stays fixed
  until the trade is closed and reconciled. While idle the model is therefore quoted
  at the reference itself;
- strikes are listed at the reference level (rounded there, never at the data's level
  and then rescaled, which would magnify the rounding), with the model's own
  strike_step; premiums, spread, ticks, stops, targets, fees and every dollar amount
  are unchanged (they are option and account values, already at the model's level);
- the bars the readers see are NOT rescaled. The one futures-points number the
  decision compares with them, the levels room N (points of futures move that make the
  profit target, G4), is converted back to the data's level: N x anchor / reference;
- the model guard (model_check.py) checks the model at the reference.

So a trade's dollar result depends only on its percentage path, and where the anchor
equals the reference nothing is rescaled at all. The live trading path never imports
this module.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from footprint.agents.levels import LevelsReader
from footprint.agents.range_reader import RangeReader
from footprint.core.config import Profiles
from footprint.decision.console.desmond import Console, ConsoleInputs, ConsoleVerdict
from footprint.decision.pipeline import Gatekeeper


def _require_price(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"the {name} must be a finite price > 0, got {value!r}")


class PriceLevel:
    """Maps the data's futures price to the price the option model is quoted at."""

    def __init__(self, reference: Decimal | None) -> None:
        if reference is not None:
            _require_price(reference, "reference price")
        self._reference = reference
        self._anchor: Decimal | None = None

    @property
    def reference(self) -> Decimal | None:
        return self._reference

    def anchor(self, price: Decimal) -> None:
        """The futures price now, while nothing is live: what the next trade's moves are
        measured from."""
        _require_price(price, "anchor price")
        self._anchor = price

    def anchored_at(self, price: Decimal) -> bool:
        """True when the anchor is `price`: anchoring at it again would change nothing."""
        return self._anchor == price

    def model_price(self, price: Decimal) -> Decimal:
        """The futures price the option model is quoted at, for the data's `price`."""
        if self._reference is None:
            return price
        return price * self._reference / self._anchored()

    def data_points(self, points: Decimal) -> Decimal:
        """A futures move of `points` at the model's level, at the data's level."""
        if self._reference is None:
            return points
        return points * self._anchored() / self._reference

    def _anchored(self) -> Decimal:
        if self._anchor is None:
            raise ValueError("no anchor price yet: nothing can be rescaled")
        return self._anchor


class LevelConsole:
    """A console that judges the levels room at the data's level: the wrapped console
    gets the decision's inputs with N converted (PriceLevel.data_points); nothing
    else changes."""

    def __init__(self, inner: Gatekeeper, level: PriceLevel) -> None:
        self.inner = inner
        self._level = level

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        return self.inner.evaluate(
            replace(inputs, n_points=self._level.data_points(inputs.n_points))
        )


def level_console(
    console: Gatekeeper | None, profiles: Profiles, level: PriceLevel
) -> Gatekeeper | None:
    """The console a backtest's pipeline gets. With no reference: `console` itself (None
    keeps the pipeline's own live console). With one: `console` (or, when None, the live
    console built as the pipeline builds it) wrapped in a LevelConsole."""
    if level.reference is None:
        return console
    return LevelConsole(live_console(profiles) if console is None else console, level)


def live_console(profiles: Profiles) -> Console:
    """The live console (Desmond), built as the pipeline builds it, for a backtest-only
    wrapper to wrap."""
    s, inst = profiles.strategy, profiles.instrument
    return Console(
        s.console, inst, LevelsReader(s.levels, s.trend, s.data, inst), RangeReader(s.range)
    )
