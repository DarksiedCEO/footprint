"""Q5.3 entry "pullback": the pullback detector, on hand-built 1-min bars.

Andre's approved definition (2026-10-10) is in docs/strategy-rules.md word for word;
how each word is read is recorded in docs/decisions-pending.md (Q5.3-a). The spike
test itself (the same one "breakout" uses) is handed in as one Score per bar here;
slice 2 feeds it from the momentum reader's spike test.

These fixtures prove the detector does what the definition says, bar by bar. They
say NOTHING about profitability.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.backtest.pullback import pullback_entries
from footprint.core.models import Bar, Score

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)  # a Wednesday
LIFE = timedelta(minutes=30)
UP, DOWN, NONE = Score.BULLISH, Score.BEARISH, Score.NEUTRAL

# (open, high, low, close): spike high 105; bar 1 runs on, bar 2 touches 105 and holds,
# bar 3 is the entry: closes up (104.5 -> 106.5) and above bar 2's high (106).
BASE = [
    ("100", "105", "100", "104"),  # 0 spike (bullish)
    ("105.5", "107", "105.5", "106"),  # 1 low above the spike high: no touch
    ("106", "106", "105", "105.5"),  # 2 touch: low 105 == spike high; close 105.5 holds
    ("105.5", "106.75", "105.25", "106.5"),  # 3 ENTRY: close > open and > 106 (bar 2 high)
    ("106.5", "108", "106", "107.5"),  # 4 would also qualify; only the first counts
]
SPIKES = [UP, NONE, NONE, NONE, NONE]


def make(rows: list[tuple[str, str, str, str]], start: datetime = T0) -> list[Bar]:
    return [
        Bar("NQ", start + i * MINUTE, Decimal(o), Decimal(h), Decimal(lo), Decimal(c), 100)
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def mirror(rows: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, str]]:
    """The down move: every price reflected through 200 (highs <-> lows)."""

    def r(x: str) -> str:
        return str(Decimal(200) - Decimal(x))

    return [(r(o), r(lo), r(h), r(c)) for o, h, lo, c in rows]


def flip(spikes: list[Score]) -> list[Score]:
    return [{UP: DOWN, DOWN: UP}.get(s, s) for s in spikes]


def entries(rows: list[tuple[str, str, str, str]], spikes: list[Score], **kw: object) -> list[int]:
    return pullback_entries(make(rows), spikes, kw.get("life", LIFE), MINUTE, rising=True)  # type: ignore[arg-type]


def both(rows: list[tuple[str, str, str, str]], spikes: list[Score]) -> tuple[list[int], list[int]]:
    up = pullback_entries(make(rows), spikes, LIFE, MINUTE, rising=True)
    down = pullback_entries(make(mirror(rows)), flip(spikes), LIFE, MINUTE, rising=False)
    return up, down


def test_fires_on_the_first_up_close_above_the_prior_high_after_the_touch() -> None:
    assert both(BASE, SPIKES) == ([3], [3])


def test_every_prefix_answers_only_on_its_own_bars() -> None:
    for n in range(1, len(BASE) + 1):
        up, down = both(BASE[:n], SPIKES[:n])
        assert up == down == ([3] if n > 3 else [])


def test_no_spike_no_setup_and_the_other_direction_does_not_fire() -> None:
    assert both(BASE, [NONE] * 5) == ([], [])
    # a bearish spike never starts the up setup
    assert entries(BASE, [DOWN, NONE, NONE, NONE, NONE]) == []
    assert pullback_entries(make(mirror(BASE)), SPIKES, LIFE, MINUTE, rising=False) == []


def test_without_a_touch_there_is_no_entry() -> None:
    rows = list(BASE)
    rows[2] = ("106", "106", "105.01", "105.5")  # low just above the spike high
    assert both(rows, SPIKES) == ([], [])


def test_the_touch_bar_itself_cannot_be_the_entry() -> None:
    rows = list(BASE)
    # bar 2 touches AND closes up above bar 1's high: still only the touch
    rows[2] = ("105", "108", "105", "107.5")
    rows[3] = ("107.5", "108", "107", "107.25")  # not above bar 2's high (108)
    rows[4] = ("107", "109", "107", "108.5")  # first up close above the prior high
    assert both(rows, SPIKES) == ([4], [4])


def test_a_close_below_the_spike_high_cancels_the_setup() -> None:
    rows = list(BASE)
    rows[2] = ("106", "106", "104", "104.99")  # touch, but closes below 105
    assert both(rows, SPIKES) == ([], [])


def test_a_close_exactly_at_the_spike_high_holds() -> None:
    rows = list(BASE)
    rows[2] = ("106", "106", "104", "105")  # closes AT 105: not below
    assert both(rows, SPIKES) == ([3], [3])


def test_an_entry_candidate_that_closes_below_the_spike_high_cancels() -> None:
    rows = [
        ("100", "105", "100", "104"),  # 0 spike
        ("104", "105", "103", "103.5"),  # 1 touch, closes below: cancels
        ("103.5", "106", "103.5", "105.5"),  # 2 up, above prior high: but the setup is gone
    ]
    assert both(rows, [UP, NONE, NONE]) == ([], [])


def test_the_entry_must_close_up() -> None:
    rows = list(BASE)
    rows[3] = ("106.75", "106.75", "105.25", "106.5")  # close > prior high, but down candle
    rows[4] = ("106.5", "106.5", "106", "106.5")  # doji: close == open
    assert both(rows, SPIKES) == ([], [])


def test_the_entry_must_close_strictly_above_the_prior_high() -> None:
    rows = list(BASE)
    rows[3] = ("105.5", "106.5", "105.25", "106")  # close == bar 2's high
    rows[4] = ("106", "106.5", "105.75", "106.25")  # close below bar 3's high
    assert both(rows, SPIKES) == ([], [])


def test_the_setup_lives_exactly_the_configured_time() -> None:
    # entry bar 3 is three minutes after the spike
    assert entries(BASE, SPIKES, life=timedelta(minutes=3)) == [3]
    assert entries(BASE, SPIKES, life=timedelta(minutes=2)) == []
    down = pullback_entries(
        make(mirror(BASE)), flip(SPIKES), timedelta(minutes=2), MINUTE, rising=False
    )
    assert down == []


def test_a_missing_bar_drops_the_setup() -> None:
    bars = make(BASE)
    gapped = bars[:3] + [
        Bar("NQ", b.ts + MINUTE, b.open, b.high, b.low, b.close, 100) for b in bars[3:]
    ]
    assert pullback_entries(gapped, SPIKES, LIFE, MINUTE, rising=True) == []


def test_each_spike_is_its_own_setup_and_fires_once() -> None:
    rows = [*BASE, ("107.5", "107.5", "105", "105.25"), ("105.25", "108.5", "105.25", "108.25")]
    # bar 4 is a second spike (high 108); bar 5 touches 105 (setup 1 is done) but not
    # 108's hold: it closes below 108, so setup 2 is cancelled; nothing fires again.
    assert both(rows, [UP, NONE, NONE, NONE, UP, NONE, NONE]) == ([3], [3])


def test_a_later_spike_fires_its_own_pullback() -> None:
    rows = [
        ("100", "101", "99", "100"),  # 0
        ("100", "105", "100", "104"),  # 1 spike, high 105
        ("104.5", "106", "104.5", "105.5"),  # 2 touch: low 104.5 <= 105; holds
        ("105.5", "106.5", "105", "106.25"),  # 3 ENTRY (close up, > 106)
    ]
    assert both(rows, [NONE, UP, NONE, NONE]) == ([3], [3])


def test_spikes_must_match_the_bars() -> None:
    with pytest.raises(ValueError, match="one spike read per bar"):
        pullback_entries(make(BASE), SPIKES[:-1], LIFE, MINUTE, rising=True)


def test_setups_live_side_by_side_and_one_entering_keeps_the_others() -> None:
    # Reading Q5.3-a (6), review G48-1: spike A is touched; spike B starts while A
    # is alive; A enters on a bar that does not touch B, and B survives that bar
    # to be touched and to enter on its own later.
    rows = [
        ("100", "105", "100", "104"),  # 0 spike A, level 105
        ("105.5", "106", "105", "105.5"),  # 1 touches A, holds
        ("105.5", "106", "105.2", "105.8"),  # 2 spike B, level 106; A: up but not > 106
        ("106.2", "107", "106.1", "106.8"),  # 3 A ENTERS (> 106); B untouched (low 106.1)
        ("106.8", "106.9", "106", "106.1"),  # 4 touches B (low 106), holds both
        ("106.1", "107.5", "106.05", "107.2"),  # 5 B ENTERS (close up, > 106.9)
    ]
    assert both(rows, [UP, NONE, UP, NONE, NONE, NONE]) == ([3, 5], [3, 5])


def test_a_repeated_timestamp_drops_the_setup() -> None:
    # Reading Q5.3-a (7), review G48-1: any step other than one interval, a zero step
    # included, drops every live setup.
    bars = make(BASE)
    repeated = bars[:3] + [
        Bar("NQ", b.ts - MINUTE, b.open, b.high, b.low, b.close, 100) for b in bars[3:]
    ]
    assert repeated[3].ts == repeated[2].ts
    assert pullback_entries(repeated, SPIKES, LIFE, MINUTE, rising=True) == []
    down = make(mirror(BASE))
    down_repeated = down[:3] + [
        Bar("NQ", b.ts - MINUTE, b.open, b.high, b.low, b.close, 100) for b in down[3:]
    ]
    assert pullback_entries(down_repeated, flip(SPIKES), LIFE, MINUTE, rising=False) == []
