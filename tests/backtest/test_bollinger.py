"""Bollinger Band entry rules (Andre's rule sheet, approved 2026-10-17; backtest only).

The three setups are tested on hand-built bars and hand-set bands so each test pins one
word of the definition: the bar it fires on, the bar just before and after, the window
edge, the cancel, the mirror. The trigger, the config loader, the rule registry, the
rule console (old confirmers OFF) and the CLI wiring are tested after. Synthetic bars
say NOTHING about profitability.
"""

from __future__ import annotations

import contextlib
import io
import random
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.__main__ import main
from footprint.backtest.bollinger import (
    Band,
    BollingerSettings,
    BollingerTrigger,
    bands,
    bounce_fires,
    squeeze_fires,
    walk_fires,
)
from footprint.backtest.price_level import LevelConsole
from footprint.backtest.rule_console import RuleConsole
from footprint.backtest.settings import BacktestSettings, load_backtest
from footprint.core.config import ConfigError, Profiles
from footprint.core.models import Bar, Direction, Read, Score
from footprint.decision.console.desmond import Console
from footprint.decision.pipeline import Pipeline
from footprint.decision.risk.gate import AccountState
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.conftest import REPO_ROOT
from tests.decision.scenario import DECISION_TIME, chain_for, market

D = Decimal
T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)

S = BollingerSettings(
    period=2,
    width=D(1),
    fast_minutes=1,
    slow_minutes=5,
    squeeze_lookback=3,
    squeeze_window=2,
    walk_lookback=3,
    walk_min_closes=2,
    walk_window=2,
    slope_bars=2,
    bounce_window=2,
)


def bar(
    i: int,
    close: float,
    *,
    o: float | None = None,
    hi: float | None = None,
    lo: float | None = None,
) -> Bar:
    c = D(str(close))
    op = c if o is None else D(str(o))
    return Bar(
        symbol="NQ",
        ts=T0 + i * MIN,
        open=op,
        high=max(op, c) if hi is None else D(str(hi)),
        low=min(op, c) if lo is None else D(str(lo)),
        close=c,
        volume=100,
    )


def flat(count: int) -> list[Band | None]:
    return [Band(D(90), D(100), D(110)) for _ in range(count)]


# --- the bands ------------------------------------------------------------------------


def test_bands_are_the_mean_plus_and_minus_width_population_deviations() -> None:
    bs = bands([bar(0, 1), bar(1, 3), bar(2, 3)], period=2, width=D(2))
    assert bs[0] is None  # not enough bars yet
    assert bs[1] == Band(D(0), D(2), D(4))  # mean 2, deviation 1, width 2
    assert bs[2] == Band(D(3), D(3), D(3))  # no spread


def test_bandwidth_is_the_band_span_over_the_middle() -> None:
    assert Band(D(90), D(100), D(110)).bandwidth == D("0.2")


# --- squeeze breakout -----------------------------------------------------------------


def squeeze_bands(widths: list[int]) -> list[Band | None]:
    return [Band(D(100) - D(w) / 2, D(100), D(100) + D(w) / 2) for w in widths]


def test_squeeze_fires_on_the_first_close_above_the_upper_band_after_a_squeeze() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20])  # bar 3 is the tightest of bars 1..3
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 111)]
    assert squeeze_fires(bars, bs, S) is Score.BULLISH


def test_squeeze_short_is_the_mirror() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20])
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 89)]
    assert squeeze_fires(bars, bs, S) is Score.BEARISH


def test_a_close_on_the_band_is_not_outside_it() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20])
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 110)]
    assert squeeze_fires(bars, bs, S) is Score.NEUTRAL


def test_a_close_on_the_lower_band_is_not_outside_it() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20])
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 90)]
    assert squeeze_fires(bars, bs, S) is Score.NEUTRAL


def test_the_second_close_outside_after_a_squeeze_does_not_fire() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20, 20])
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 111), bar(5, 112)]
    assert squeeze_fires(bars, bs, S) is Score.NEUTRAL


def test_the_second_close_below_after_a_squeeze_does_not_fire() -> None:
    bs = squeeze_bands([20, 20, 20, 10, 20, 20])
    bars = [bar(i, 100) for i in range(4)] + [bar(4, 89), bar(5, 88)]
    assert squeeze_fires(bars, bs, S) is Score.NEUTRAL


def test_a_breakout_at_the_window_edge_fires_one_bar_later_does_not() -> None:
    edge = squeeze_bands([20, 20, 20, 10, 20, 20])  # squeeze at 3, breakout at 5 = 2 bars
    assert squeeze_fires([bar(i, 100) for i in range(5)] + [bar(5, 111)], edge, S) is (
        Score.BULLISH
    )
    late = squeeze_bands([20, 20, 20, 10, 20, 20, 20])  # breakout at 6 = 3 bars after
    assert squeeze_fires([bar(i, 100) for i in range(6)] + [bar(6, 111)], late, S) is (
        Score.NEUTRAL
    )


def test_a_breakout_without_a_squeeze_does_not_fire() -> None:
    rising = squeeze_bands([10, 12, 14, 16, 18, 20])  # every bar wider than the one before
    assert squeeze_fires([bar(i, 100) for i in range(5)] + [bar(5, 111)], rising, S) is (
        Score.NEUTRAL
    )


def test_a_squeeze_needs_a_full_lookback_of_bands() -> None:
    bs: list[Band | None] = [None, *squeeze_bands([20, 10, 20])]
    bars = [bar(i, 100) for i in range(3)] + [bar(3, 111)]
    assert squeeze_fires(bars, bs, S) is Score.NEUTRAL


# --- band-walk pullback ---------------------------------------------------------------


def walk_bars(*rest: Bar) -> list[Bar]:
    # bars 0 and 1 close on/above the upper band; bar 2 dips to the middle line
    return [bar(0, 111), bar(1, 110), bar(2, 105, hi=106, lo=100), *rest]


def test_walk_fires_on_the_first_close_above_the_middle_and_the_prior_high() -> None:
    bars = walk_bars(bar(3, 107))
    assert walk_fires(bars, flat(len(bars)), S) is Score.BULLISH


def test_walk_does_not_fire_on_the_touch_bar_itself() -> None:
    bars = walk_bars()
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_walk_close_not_above_the_prior_high_waits_and_the_next_one_fires() -> None:
    first = walk_bars(bar(3, 106, hi=106.5))
    assert walk_fires(first, flat(len(first)), S) is Score.NEUTRAL
    second = walk_bars(bar(3, 106, hi=106.5), bar(4, 107))
    assert walk_fires(second, flat(len(second)), S) is Score.BULLISH


def test_walk_entry_after_the_window_does_not_fire() -> None:
    bars = walk_bars(bar(3, 105, hi=105), bar(4, 104, hi=104), bar(5, 107))
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_walk_needs_the_minimum_closes_on_the_band() -> None:
    bars = [bar(0, 111), bar(1, 109), bar(2, 105, hi=106, lo=100), bar(3, 107)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_walk_needs_a_touch_of_the_middle_line() -> None:
    bars = [bar(0, 111), bar(1, 110), bar(2, 105, hi=106, lo=101), bar(3, 107)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_a_close_below_the_lower_band_cancels_the_walk() -> None:
    bars = walk_bars(bar(3, 89), bar(4, 107, o=100))
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_a_break_cannot_be_skipped_by_a_later_touch() -> None:
    # bar 2 closes below the lower band; bar 3 dips to the middle after it (hostile review)
    bars = [bar(0, 111), bar(1, 110), bar(2, 89), bar(3, 95, hi=96, lo=94), bar(4, 107)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_a_short_walk_is_cancelled_by_a_close_above_the_upper_band() -> None:
    on_touch = [bar(0, 89), bar(1, 90), bar(2, 95, hi=100, lo=94), bar(3, 111), bar(4, 93)]
    assert walk_fires(on_touch, flat(5), S) is Score.NEUTRAL
    later = [bar(0, 89), bar(1, 90), bar(2, 111), bar(3, 105, hi=106, lo=104), bar(4, 93)]
    assert walk_fires(later, flat(5), S) is Score.NEUTRAL


def test_walk_closes_count_back_exactly_the_lookback() -> None:
    # touch at bar 3; the lookback is bars 0..2, so the walk close on bar 0 counts
    edge = [bar(0, 111), bar(1, 105), bar(2, 110), bar(3, 105, hi=106, lo=100), bar(4, 107)]
    assert walk_fires(edge, flat(5), S) is Score.BULLISH
    # one bar further back (touch at bar 4, walk close on bar 0) does not
    late = [bar(0, 111), bar(1, 105), bar(2, 105), bar(3, 110)]
    late += [bar(4, 105, hi=106, lo=100), bar(5, 107)]
    assert walk_fires(late, flat(6), S) is Score.NEUTRAL


def test_only_the_first_walk_entry_fires() -> None:
    bars = walk_bars(bar(3, 107), bar(4, 108))
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_walk_short_is_the_mirror() -> None:
    bars = [bar(0, 89), bar(1, 90), bar(2, 95, hi=100, lo=94), bar(3, 93)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.BEARISH


def test_walk_short_needs_a_close_below_the_prior_low() -> None:
    bars = [bar(0, 89), bar(1, 90), bar(2, 95, hi=100, lo=94), bar(3, 95)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL


def test_walk_short_close_on_the_middle_line_does_not_confirm() -> None:
    bars = [bar(0, 89), bar(1, 90), bar(2, 102, hi=103, lo=101), bar(3, 100)]
    assert walk_fires(bars, flat(len(bars)), S) is Score.NEUTRAL  # middle is 100


def test_a_close_on_the_lower_band_does_not_cancel_the_walk() -> None:
    bars = walk_bars(bar(3, 90), bar(4, 107))
    assert walk_fires(bars, flat(len(bars)), S) is Score.BULLISH  # lower band is 90


def test_an_entry_bar_does_not_arm_a_second_walk_entry() -> None:
    # bar 3 fires and also dips to the middle; one setup gives one entry (Andre)
    bars = walk_bars(bar(3, 107, hi=107, lo=100))
    assert walk_fires(bars, flat(len(bars)), S) is Score.BULLISH
    again = [*bars, bar(4, 108)]
    assert walk_fires(again, flat(len(again)), S) is Score.NEUTRAL


def test_a_touch_that_looks_like_an_entry_but_did_not_fire_still_arms_the_walk() -> None:
    # bar 2 dips to the middle and closes above bar 1's high, with no earlier touch
    bars = [bar(0, 111), bar(1, 110), bar(2, 111, hi=112, lo=100), bar(3, 113)]
    assert walk_fires(bars[:3], flat(3), S) is Score.NEUTRAL
    assert walk_fires(bars, flat(4), S) is Score.BULLISH


def test_a_break_inside_the_lookback_resets_the_walk_count() -> None:
    # walk close, break, walk close, touch: only one close since the break (Andre)
    bars = [bar(0, 111), bar(1, 89), bar(2, 110), bar(3, 105, hi=106, lo=100), bar(4, 107)]
    assert walk_fires(bars, flat(5), S) is Score.NEUTRAL
    mirror = [bar(0, 89), bar(1, 111), bar(2, 90), bar(3, 95, hi=100, lo=94), bar(4, 93)]
    assert walk_fires(mirror, flat(5), S) is Score.NEUTRAL


def test_a_fresh_walk_after_a_break_fires() -> None:
    bars = [bar(0, 89), bar(1, 111), bar(2, 110), bar(3, 105, hi=106, lo=100), bar(4, 107)]
    assert walk_fires(bars, flat(5), S) is Score.BULLISH


def test_the_walk_closes_before_a_break_do_not_hide_a_fresh_walk() -> None:
    # walk close, break, two fresh walk closes, touch, entry: the fresh walk fires
    long = replace(S, walk_lookback=4)
    bars = [bar(0, 111), bar(1, 89), bar(2, 111), bar(3, 110)]
    bars += [bar(4, 105, hi=106, lo=100), bar(5, 107)]
    assert walk_fires(bars, flat(6), long) is Score.BULLISH


def rising_bands(count: int, step: int = 1) -> list[Band | None]:
    return [Band(D(90 + i * step), D(100 + i * step), D(110 + i * step)) for i in range(count)]


def test_bounce_fires_on_the_first_up_close_back_inside_after_a_lower_band_touch() -> None:
    bars = [bar(0, 100), bar(1, 101), bar(2, 93, lo=92), bar(3, 103, o=100)]
    assert bounce_fires(bars, rising_bands(4), S) is Score.BULLISH


def test_bounce_needs_a_rising_middle_line() -> None:
    # a real touch of the lower band and an up close back inside: only the slope is missing
    bars = [bar(0, 100), bar(1, 101), bar(2, 91, lo=89), bar(3, 103, o=100)]
    assert bounce_fires(bars, flat(4), S) is Score.NEUTRAL


def test_bounce_needs_an_up_candle() -> None:
    bars = [bar(0, 100), bar(1, 101), bar(2, 93, lo=92), bar(3, 100, o=103)]
    assert bounce_fires(bars, rising_bands(4), S) is Score.NEUTRAL


def test_bounce_needs_the_close_back_inside() -> None:
    bars = [bar(0, 100), bar(1, 101), bar(2, 93, lo=92), bar(3, 93, o=91)]
    assert bounce_fires(bars, rising_bands(4), S) is Score.NEUTRAL  # lower band is 93


def test_only_the_first_bounce_entry_fires() -> None:
    bars = [bar(0, 100), bar(1, 93, lo=91), bar(2, 101, o=99), bar(3, 104, o=102)]
    assert bounce_fires(bars, rising_bands(4), S) is Score.NEUTRAL


def test_a_touch_older_than_the_window_does_not_count() -> None:
    bars = [bar(0, 100), bar(1, 91, lo=90), bar(2, 100), bar(3, 101), bar(4, 104, o=102)]
    assert bounce_fires(bars, rising_bands(5), S) is Score.NEUTRAL


def test_bounce_short_is_the_mirror() -> None:
    bars = [bar(0, 100), bar(1, 99), bar(2, 107, hi=108), bar(3, 97, o=100)]
    falling = [Band(D(90 - i), D(100 - i), D(110 - i)) for i in range(4)]
    assert bounce_fires(bars, falling, S) is Score.BEARISH


def test_a_short_bounce_closing_on_the_upper_band_is_not_back_inside() -> None:
    bars = [bar(0, 100), bar(1, 99), bar(2, 107, hi=108), bar(3, 107, o=109)]
    falling = [Band(D(90 - i), D(100 - i), D(110 - i)) for i in range(4)]
    assert bounce_fires(bars, falling, S) is Score.NEUTRAL  # upper band at bar 3 is 107


def test_a_doji_is_neither_an_up_nor_a_down_candle() -> None:
    falling = [Band(D(90 - i), D(100 - i), D(110 - i)) for i in range(4)]
    short = [bar(0, 100), bar(1, 99), bar(2, 107, hi=108), bar(3, 100, o=100)]
    assert bounce_fires(short, falling, S) is Score.NEUTRAL
    long = [bar(0, 100), bar(1, 101), bar(2, 93, lo=92), bar(3, 100, o=100)]
    assert bounce_fires(long, rising_bands(4), S) is Score.NEUTRAL


def test_a_touch_at_the_window_edge_counts() -> None:
    # touch on bar 2, entry on bar 4: two bars back, the edge of a two-bar window
    bars = [bar(0, 100), bar(1, 101), bar(2, 93, lo=92), bar(3, 99, o=100), bar(4, 104, o=102)]
    assert bounce_fires(bars, rising_bands(5), S) is Score.BULLISH


def test_the_slope_is_measured_exactly_slope_bars_back() -> None:
    # the middle is 100 two bars back and now, 99 one bar back: flat, so no bounce
    bs: list[Band | None] = [Band(D(m - 10), D(m), D(m + 10)) for m in (100, 100, 99, 100)]
    bars = [bar(0, 100), bar(1, 101), bar(2, 90, lo=88), bar(3, 103, o=100)]
    assert bounce_fires(bars, bs, S) is Score.NEUTRAL


def test_an_entry_bar_does_not_arm_a_second_bounce_entry() -> None:
    # bar 2 touches the lower band and fires; one setup gives one entry (Andre)
    bars = [bar(0, 100), bar(1, 90, lo=90), bar(2, 95, o=93, lo=91)]
    assert bounce_fires(bars, rising_bands(3), S) is Score.BULLISH
    again = [*bars, bar(3, 104, o=102)]
    assert bounce_fires(again, rising_bands(4), S) is Score.NEUTRAL


def test_a_hammer_that_did_not_fire_still_arms_the_bounce() -> None:
    # bar 2 touches the lower band and closes back inside up, with no earlier touch
    bars = [bar(0, 100), bar(1, 101), bar(2, 95, o=93, lo=91)]
    assert bounce_fires(bars, rising_bands(3), S) is Score.NEUTRAL
    again = [*bars, bar(3, 104, o=102)]
    assert bounce_fires(again, rising_bands(4), S) is Score.BULLISH


# --- the trigger ----------------------------------------------------------------------


def minutes(count: int, start: datetime = T0) -> list[Bar]:
    return [replace(bar(i, 100 + i % 3), ts=start + i * MIN) for i in range(count)]


def test_the_slow_trigger_answers_only_when_its_bar_just_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger = BollingerTrigger(S, "squeeze", S.slow_minutes)
    seen: list[list[Bar]] = []

    def spy(bars: list[Bar], bs: list[Band | None], s: BollingerSettings) -> Score:
        seen.append(list(bars))
        return Score.BULLISH

    monkeypatch.setattr(trigger, "_detect", spy)
    inside = trigger.read(minutes(13))  # last minute 14:12, the 14:10 bar is still open
    assert inside.score is Score.NEUTRAL
    assert seen == []
    done = trigger.read(minutes(15))  # last minute 14:14 completes the 14:10 bar
    assert done.score is Score.BULLISH
    assert done.agent_id == "momentum"
    assert [b.ts for b in seen[-1]][-1] == T0 + 10 * MIN
    assert all(b.ts.minute % 5 == 0 for b in seen[-1])
    assert len(seen[-1]) <= S.history
    full = trigger.read(minutes(S.history * S.slow_minutes + 20))
    assert full.score is Score.BULLISH
    assert len(seen[-1]) == S.history  # history five-minute bars, not one-minute ones


def test_history_reaches_back_the_longest_setup() -> None:
    squeeze = replace(S, squeeze_lookback=50)
    assert squeeze.history == S.period + S.squeeze_window + 50 + 1
    walk = replace(S, walk_lookback=50)
    assert walk.history == S.period + S.walk_window + 50 + 1
    assert replace(S, bounce_window=70).history == S.period + 70 + 1
    assert replace(S, slope_bars=70).history == S.period + 70 + 1


def test_the_fast_trigger_answers_every_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    trigger = BollingerTrigger(S, "walk", S.fast_minutes)
    monkeypatch.setattr(trigger, "_detect", lambda bars, bs, s: Score.BEARISH)
    for count in (7, 8, 9):
        assert trigger.read(minutes(count)).score is Score.BEARISH


def test_the_trigger_hands_the_detector_bands_from_its_own_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger = BollingerTrigger(S, "bounce", S.fast_minutes)
    got: list[list[Band | None]] = []

    def spy(bars: list[Bar], bs: list[Band | None], s: BollingerSettings) -> Score:
        got.append(list(bs))
        assert s is S
        return Score.NEUTRAL

    monkeypatch.setattr(trigger, "_detect", spy)
    bars = minutes(6)
    trigger.read(bars)
    assert got[-1] == bands(bars[-S.history :], S.period, S.width)


def test_unknown_setups_and_bad_timeframes_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown bollinger setup"):
        BollingerTrigger(S, "nope", S.fast_minutes)
    with pytest.raises(ValueError, match="at least one minute"):
        BollingerTrigger(S, "walk", 0)


def test_settings_refuse_impossible_numbers() -> None:
    with pytest.raises(ValueError, match="period"):
        replace(S, period=1)
    with pytest.raises(ValueError, match="width"):
        replace(S, width=D(0))
    with pytest.raises(ValueError, match="walk_min_closes"):
        replace(S, walk_min_closes=S.walk_lookback + 1)
    with pytest.raises(ValueError, match="bounce_window"):
        replace(S, bounce_window=0)
    with pytest.raises(ValueError, match="fast_minutes must be shorter"):
        replace(S, fast_minutes=S.slow_minutes)


def random_walk(count: int, seed: int) -> list[Bar]:
    """Calm and wild stretches in turn, so bands squeeze, walk and get touched."""
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 20000.0
    for i in range(count):
        vol = 2.0 if (i // 150) % 2 else 12.0
        o = price
        price += rng.gauss(0, vol)
        hi = max(o, price) + abs(rng.gauss(0, vol / 2))
        lo = min(o, price) - abs(rng.gauss(0, vol / 2))
        out.append(
            Bar(
                symbol="NQ",
                ts=T0 + i * MIN,
                open=D(f"{o:.2f}"),
                high=D(f"{hi:.2f}"),
                low=D(f"{lo:.2f}"),
                close=D(f"{price:.2f}"),
                volume=100,
            )
        )
    return out


WALK = random_walk(3000, seed=7)


@pytest.mark.parametrize("setup", ["squeeze", "walk", "bounce"])
@pytest.mark.parametrize("slow", [False, True])
def test_each_setup_fires_long_and_short_on_the_config_numbers(
    setup: str, slow: bool, settings: BacktestSettings
) -> None:
    b = settings.rules.bollinger
    span = b.slow_minutes if slow else b.fast_minutes
    trigger = BollingerTrigger(b, setup, span)
    need = b.history * span
    seen: set[Score] = set()
    for end in range(need, len(WALK) + 1, span):  # each completed bar of the timeframe
        seen.add(trigger.read(WALK[end - need : end]).score)
        if {Score.BULLISH, Score.BEARISH} <= seen:
            break
    assert {Score.BULLISH, Score.BEARISH} <= seen


# --- config and registry --------------------------------------------------------------


def test_the_numbers_load_from_the_backtest_file(settings: BacktestSettings) -> None:
    b = settings.rules.bollinger
    assert b.period > 1
    assert b.width > 0
    assert b.fast_minutes < b.slow_minutes


def test_a_bad_bollinger_number_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "backtest.yaml"
    text = (REPO_ROOT / "config" / "backtest.yaml").read_text()
    path.write_text(text.replace("walk_min_closes: 3", "walk_min_closes: 99"))
    with pytest.raises(ConfigError, match="rules: bollinger: "):
        load_backtest(path)


def test_an_engine_history_too_short_for_the_rules_is_a_config_error(
    tmp_path: Path, settings: BacktestSettings
) -> None:
    b = settings.rules.bollinger
    need = b.history * b.slow_minutes
    path = tmp_path / "backtest.yaml"
    text = (REPO_ROOT / "config" / "backtest.yaml").read_text()
    assert "history_minutes: 43200" in text
    path.write_text(text.replace("history_minutes: 43200", f"history_minutes: {need}"))
    assert load_backtest(path).engine.history_minutes == need  # exactly enough is fine
    path.write_text(text.replace("history_minutes: 43200", f"history_minutes: {need - 1}"))
    with pytest.raises(ConfigError, match="history_minutes must cover the bollinger rules"):
        load_backtest(path)


BB = [f"bb_{s}_{v}" for s in ("bounce", "squeeze", "walk") for v in ("fast", "slow")]


def test_the_six_bollinger_rules_are_registered_and_run_with_confirmers_off() -> None:
    assert sorted(n for n in rules.ENTRY_RULES if n.startswith("bb_")) == BB
    assert frozenset(BB) == rules.GATELESS_ENTRY_RULES


@pytest.mark.parametrize("name", BB)
def test_each_rule_builds_its_setup_on_its_bar_length(
    name: str, profiles: Profiles, settings: BacktestSettings
) -> None:
    trigger = rules.ENTRY_RULES[name](profiles, settings.rules)
    assert isinstance(trigger, BollingerTrigger)
    b = settings.rules.bollinger
    assert trigger._setup == name.split("_")[1]
    assert trigger._minutes == (b.slow_minutes if name.endswith("_slow") else b.fast_minutes)
    assert trigger._s is b
    numbers = rules.entry_rule_numbers(name, settings.rules)
    assert numbers["rule bollinger period"] == str(b.period)


# --- the rule console: the old confirmers are OFF -------------------------------------


class Always:
    def __init__(self, score: Score) -> None:
        self.score = score

    @property
    def agent_id(self) -> str:
        return "momentum"

    def read(self, bars: object) -> Read:
        return Read("momentum", DECISION_TIME, self.score, "stub")


def account(profiles: Profiles) -> AccountState:
    bank = profiles.risk.bankroll
    return AccountState(bank, bank, D(0), D(0), 0, 0, 0, False, False)


def decide(profiles: Profiles, score: Score, console: bool, bars: list[Bar]) -> object:
    closed = [b for b in bars if b.ts + MIN <= DECISION_TIME]
    rule_console = RuleConsole(profiles.strategy.console, profiles.instrument)
    pipeline = Pipeline(profiles, Always(score), rule_console if console else None)
    return pipeline.decide(
        bars, chain_for(closed[-1].close, DECISION_TIME), account(profiles), (), DECISION_TIME
    )


def test_a_long_against_the_trend_fires_with_the_rule_console_only(profiles: Profiles) -> None:
    down = market(mirror=True)  # a downtrend: the live console refuses a long (NB-14)
    live = decide(profiles, Score.BULLISH, False, down)
    assert not live.fired  # type: ignore[attr-defined]
    ruled = decide(profiles, Score.BULLISH, True, down)
    assert ruled.fired, ruled.reasons()  # type: ignore[attr-defined]
    assert ruled.approved.signal.direction is Direction.LONG  # type: ignore[attr-defined]


def test_the_rule_console_needs_a_direction(profiles: Profiles) -> None:
    ruled = decide(profiles, Score.NEUTRAL, True, market())
    assert not ruled.fired  # type: ignore[attr-defined]
    assert "rule_has_direction" in [g.name for g in ruled.verdict.failed]  # type: ignore[attr-defined]


def test_the_rule_console_keeps_the_session_gate(profiles: Profiles) -> None:
    console = RuleConsole(profiles.strategy.console, profiles.instrument)
    night = DECISION_TIME.replace(hour=(DECISION_TIME.hour + 12) % 24)
    from footprint.agents.zero_day import ZeroDayCheck
    from footprint.decision.console.desmond import ConsoleInputs

    read = Read("momentum", night, Score.BULLISH, "stub")
    verdict = console.evaluate(
        ConsoleInputs(
            as_of=night,
            trend=read,
            momentum=read,
            volume=read,
            zero_day=ZeroDayCheck(night, False, ()),
            levels=None,  # type: ignore[arg-type]
            range_days=None,
            n_points=D(1),
        )
    )
    assert not verdict.go
    assert "prime_session" in [g.name for g in verdict.failed]


def test_the_rule_console_keeps_the_close_lead_gate(profiles: Profiles) -> None:
    from footprint.agents.zero_day import ZeroDayCheck
    from footprint.core.calendar import entry_cutoff
    from footprint.decision.console.desmond import ConsoleInputs

    console = RuleConsole(profiles.strategy.console, profiles.instrument)
    lead = profiles.strategy.console.close_lead_minutes
    cutoff = entry_cutoff(DECISION_TIME, profiles.instrument, lead).astimezone(UTC)

    def failed(when: datetime) -> list[str]:
        read = Read("momentum", when, Score.BULLISH, "stub")
        verdict = console.evaluate(
            ConsoleInputs(
                as_of=when,
                trend=read,
                momentum=read,
                volume=read,
                zero_day=ZeroDayCheck(when, False, ()),
                levels=None,  # type: ignore[arg-type]
                range_days=None,
                n_points=D(1),
            )
        )
        return [g.name for g in verdict.failed]

    assert failed(cutoff - MIN) == []
    assert failed(cutoff) == ["before_close_lead"]


def test_with_no_console_named_the_pipeline_builds_the_live_console(profiles: Profiles) -> None:
    assert type(Pipeline(profiles)._console) is Console
    assert type(Pipeline(profiles, Always(Score.NEUTRAL))._console) is Console


# --- the CLI --------------------------------------------------------------------------


FROM = ["--trade-from", TRADE_FROM.isoformat()]


def test_a_bollinger_run_records_that_the_confirmers_are_off(tmp_path: Path) -> None:
    bars_csv = tmp_path / "bars.csv"
    write_bars(bars_csv)
    out = tmp_path / "run"
    with contextlib.redirect_stdout(io.StringIO()):
        main(["--bars", str(bars_csv), "--out", str(out), "--entry", "bb_walk_fast", *FROM])
    readout = (out / "readout.md").read_text()
    assert "- confirmers: OFF" in readout
    assert "- rule bollinger period:" in readout
    shutil.rmtree(out)
    with contextlib.redirect_stdout(io.StringIO()):
        main(["--bars", str(bars_csv), "--out", str(out), "--entry", "pullback", *FROM])
    assert "confirmers" not in (out / "readout.md").read_text()


def test_a_bollinger_run_builds_its_pipeline_with_the_rule_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from footprint.backtest import engine

    consoles: list[object] = []
    real = Pipeline

    def spy(*args: object, **kwargs: object) -> Pipeline:
        consoles.append(args[2] if len(args) > 2 else kwargs.get("console"))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "Pipeline", spy)
    bars_csv = tmp_path / "bars.csv"
    write_bars(bars_csv)
    with contextlib.redirect_stdout(io.StringIO()):
        main(
            [
                "--bars",
                str(bars_csv),
                "--out",
                str(tmp_path / "bb"),
                "--entry",
                "bb_bounce_slow",
                *FROM,
            ]
        )
        main(["--bars", str(bars_csv), "--out", str(tmp_path / "pb"), "--entry", "pullback", *FROM])
    # The shipped config rescales option prices (BH-2): each console comes wrapped in a
    # LevelConsole, which only converts the levels room N (the rule console reads none).
    wrapped = [c.inner if isinstance(c, LevelConsole) else c for c in consoles]
    assert all(isinstance(c, LevelConsole) for c in consoles)
    assert isinstance(wrapped[0], RuleConsole)
    assert isinstance(wrapped[1], Console)  # the live console, built as the pipeline builds it
