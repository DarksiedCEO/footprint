"""Price-level rescaling for the backtest only (backtest-history-v1, BH-2).

The option model prices options at a fixed premium whatever the futures price, so it
is only meaningful near the level it was set for (GV-3/GV-4). With
`option_model.reference_price` set, the backtest quotes every option as if the future
traded at that level: each trade's futures moves are rescaled by
reference_price / anchor, where the anchor is the futures price at the last moment
nothing was live (no working entry, no position, no reconcile), i.e. the price its
entry was decided at. The bars the readers see are never rescaled; the levels room N
(futures points that make the profit target) is put back to the data's level.

Unit level here; the engine level is in test_backtest_history.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from footprint.backtest.price_level import LevelConsole, PriceLevel, level_console
from footprint.core.config import Profiles
from footprint.core.models import Read, Score
from footprint.decision.console.desmond import ConsoleInputs, ConsoleVerdict

D = Decimal
REF = D(20000)
NOW = datetime(2026, 9, 15, 14, 1, tzinfo=UTC)


def test_with_no_reference_nothing_is_rescaled() -> None:
    level = PriceLevel(None)
    assert level.reference is None
    level.anchor(D("1831.75"))
    assert level.model_price(D("1840.25")) == D("1840.25")
    assert level.data_points(D("111.5")) == D("111.5")


def test_at_the_anchor_the_model_sees_the_reference_exactly() -> None:
    level = PriceLevel(REF)
    for anchor in (D("1831.75"), D("7123.5"), D("23716.0"), D("19999.75")):
        level.anchor(anchor)
        assert level.model_price(anchor) == REF


def test_moves_are_rescaled_by_reference_over_anchor() -> None:
    level = PriceLevel(REF)
    level.anchor(D(2000))
    assert level.model_price(D(2010)) == D(20100)  # +0.5% at the data's level = +0.5% here
    assert level.model_price(D(1990)) == D(19900)
    assert level.data_points(D(100)) == D(10)  # 100 points at 20,000 are 10 at 2,000
    level.anchor(D(40000))
    assert level.model_price(D(40100)) == D(20050)
    assert level.data_points(D(100)) == D(200)


def test_when_the_anchor_is_the_reference_nothing_moves() -> None:
    level = PriceLevel(REF)
    level.anchor(REF)
    for price in (D("19876.25"), D("20000"), D("20123.75")):
        assert level.model_price(price) == price
    assert level.data_points(D("111.11")) == D("111.11")


def test_the_anchor_is_the_last_one_given() -> None:
    level = PriceLevel(REF)
    level.anchor(D(2000))
    level.anchor(D(4000))
    assert level.model_price(D(4000)) == REF
    assert level.model_price(D(2000)) == D(10000)


@pytest.mark.parametrize("bad", [D(0), D(-1), D("NaN"), D("Infinity")])
def test_a_bad_reference_or_anchor_is_refused(bad: Decimal) -> None:
    with pytest.raises(ValueError, match="reference"):
        PriceLevel(bad)
    level = PriceLevel(REF)
    with pytest.raises(ValueError, match="anchor"):
        level.anchor(bad)


def test_no_price_is_rescaled_before_an_anchor() -> None:
    level = PriceLevel(REF)
    with pytest.raises(ValueError, match="no anchor"):
        level.model_price(D(2000))
    with pytest.raises(ValueError, match="no anchor"):
        level.data_points(D(100))
    unscaled = PriceLevel(None)
    assert unscaled.model_price(D(2000)) == D(2000)  # nothing to anchor


class Recorder:
    """A stand-in console: records the inputs it was given."""

    def __init__(self) -> None:
        self.seen: list[ConsoleInputs] = []

    def evaluate(self, inputs: ConsoleInputs) -> ConsoleVerdict:
        self.seen.append(inputs)
        return ConsoleVerdict(NOW, None, None, (), (), 0, 0)  # type: ignore[arg-type]


def _inputs(n_points: Decimal) -> ConsoleInputs:
    read = Read("trend", NOW, Score.NEUTRAL, "x")
    return ConsoleInputs(NOW, read, read, read, None, None, None, n_points)  # type: ignore[arg-type]


def test_the_console_judges_room_at_the_datas_level() -> None:
    inner = Recorder()
    level = PriceLevel(REF)
    level.anchor(D(2000))
    console = LevelConsole(inner, level)
    given = _inputs(D("111.11"))
    verdict = console.evaluate(given)
    assert verdict is not None
    (seen,) = inner.seen
    assert seen.n_points == D("11.111")
    assert seen == replace(given, n_points=D("11.111"))  # nothing else changed


def test_the_console_follows_the_anchor_of_each_decision() -> None:
    inner = Recorder()
    level = PriceLevel(REF)
    console = LevelConsole(inner, level)
    level.anchor(D(2000))
    console.evaluate(_inputs(D(100)))
    level.anchor(D(10000))
    console.evaluate(_inputs(D(100)))
    assert [i.n_points for i in inner.seen] == [D(10), D(50)]


def test_with_no_reference_the_pipelines_own_console_is_kept(profiles: Profiles) -> None:
    assert level_console(None, profiles, PriceLevel(None)) is None
    inner = Recorder()
    assert level_console(inner, profiles, PriceLevel(None)) is inner


def test_with_a_reference_the_console_is_wrapped(profiles: Profiles) -> None:
    inner = Recorder()
    wrapped = level_console(inner, profiles, PriceLevel(REF))
    assert isinstance(wrapped, LevelConsole)
    assert wrapped.inner is inner
    live = level_console(None, profiles, PriceLevel(REF))
    assert isinstance(live, LevelConsole)
    assert type(live.inner).__name__ == "Console"  # the live console, Desmond
