"""Q5.2 slice 2a: the stair detector as an EntryTrigger (the momentum slot of the
decision pipeline), backtest rule library only.

The trigger answers for the LAST bar it is handed: BULLISH when the up stair enters on
it, BEARISH when the down stair does, NEUTRAL otherwise. It scans only from the last
missing bar onward (a missing bar drops every stair), and that must give exactly the
answer a scan of the whole history gives. Synthetic bars say NOTHING about
profitability.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.backtest import stairstep
from footprint.backtest.stairstep import StairTrigger, fires_on_last, stair_entries
from footprint.core.models import Bar, Score
from tests.backtest.test_stairstep import ENTRY, LOOSE, MINUTE, STAIR, T0, edit, make, mirror


def trigger(*, vol: bool = False) -> StairTrigger:
    return StairTrigger(LOOSE, MINUTE, with_volume=vol)


def test_reads_bullish_only_on_the_up_entry_bar() -> None:
    bars = make(STAIR)
    for end in range(1, len(bars) + 1):
        read = trigger().read(bars[:end])
        assert read.ts == bars[end - 1].ts
        assert read.agent_id == "momentum"
        assert read.score is (Score.BULLISH if end - 1 == ENTRY else Score.NEUTRAL)


def test_reads_bearish_only_on_the_down_entry_bar() -> None:
    bars = make(mirror(STAIR))
    for end in range(1, len(bars) + 1):
        read = trigger().read(bars[:end])
        assert read.score is (Score.BEARISH if end - 1 == ENTRY else Score.NEUTRAL)


def test_stairstep_vol_adds_the_volume_test() -> None:
    # Pause 1 heavier than push 1: the plain rule still enters, the _vol rule does not.
    rows = edit(STAIR, 3, v=300)
    assert trigger().read(make(rows)).score is Score.BULLISH
    assert trigger(vol=True).read(make(rows)).score is Score.NEUTRAL
    assert trigger(vol=True).read(make(STAIR)).score is Score.BULLISH
    assert trigger().read(make(mirror(rows))).score is Score.BEARISH
    assert trigger(vol=True).read(make(mirror(rows))).score is Score.NEUTRAL


def test_both_directions_on_one_bar_is_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed: no fixture is known where both stairs enter on one bar; if they ever
    # did, the trigger takes neither side.
    monkeypatch.setattr(stairstep, "fires_on_last", lambda *a, **k: True)
    read = trigger().read(make(STAIR))
    assert read.score is Score.NEUTRAL
    assert "both" in read.reason


def test_empty_bars_are_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        trigger().read([])


def _noise(seed: int, count: int) -> list[Bar]:
    """Contiguous 1-min bars with small random moves, broken by random gaps (1 min to
    2 days), with the base stair (up or mirrored) dropped in now and then."""
    rnd = random.Random(seed)
    bars: list[Bar] = []
    ts = T0
    price = Decimal(1000)
    while len(bars) < count:
        roll = rnd.random()
        if roll < 0.03:
            ts += MINUTE * rnd.choice([1, 2, 60, 2 * 24 * 60])
        if roll > 0.97:
            rows = STAIR if rnd.random() < 0.5 else mirror(STAIR)
            first = Decimal(rows[0][1])
            for hi, lo, c, v in rows:
                shift = price - first
                bars.append(
                    Bar(
                        "NQ",
                        ts,
                        Decimal(lo) + shift,
                        Decimal(hi) + shift,
                        Decimal(lo) + shift,
                        Decimal(c) + shift,
                        v,
                    )
                )
                ts += MINUTE
            continue
        move = Decimal(rnd.randint(-3, 3))
        high = price + Decimal(rnd.randint(0, 3))
        low = price - Decimal(rnd.randint(0, 3))
        close = min(max(price + move, low), high)
        bars.append(Bar("NQ", ts, low, high, low, close, rnd.randint(50, 300)))
        price = close
        ts += MINUTE
    return bars


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("vol", [False, True])
def test_the_windowed_answer_equals_the_full_history_answer(seed: int, vol: bool) -> None:
    bars = _noise(seed, 600)
    full_up = set(stair_entries(bars, LOOSE, MINUTE, rising=True, with_volume=vol))
    full_down = set(stair_entries(bars, LOOSE, MINUTE, rising=False, with_volume=vol))
    assert full_up  # the fixture exercises both directions
    assert full_down
    for end in range(1, len(bars) + 1):
        last = end - 1
        assert fires_on_last(bars[:end], LOOSE, MINUTE, rising=True, with_volume=vol) is (
            last in full_up
        )
        assert fires_on_last(bars[:end], LOOSE, MINUTE, rising=False, with_volume=vol) is (
            last in full_down
        )


def test_the_scan_starts_after_the_last_missing_bar() -> None:
    # A stair whose third pause is cut by a missing minute never enters, however the
    # bars before the gap looked; the same bars without the gap do.
    bars = make(STAIR)
    cut = [*bars[:10], *[replace(b, ts=b.ts + MINUTE) for b in bars[10:]]]
    assert fires_on_last(bars, LOOSE, MINUTE, rising=True, with_volume=False)
    assert not fires_on_last(cut, LOOSE, MINUTE, rising=True, with_volume=False)
    # A stair that starts right on the first bar after a gap is found (the scan
    # includes the bar after the gap).
    lead = make([("200", "190", "195", 100)], T0 - timedelta(days=2))
    assert fires_on_last([*lead, *bars], LOOSE, MINUTE, rising=True, with_volume=False)


def test_a_stair_whose_first_push_starts_on_the_very_first_bar_is_found() -> None:
    # Pause 1 low 101 gives back exactly 50% of push 1 measured from bar 0's low (98):
    # valid only if bar 0 is the start bar, so the scan must include the first bar.
    rows = edit(STAIR, 3, lo="101")
    assert stair_entries(make(rows), LOOSE, MINUTE, rising=True, with_volume=False) == [ENTRY]
    assert fires_on_last(make(rows), LOOSE, MINUTE, rising=True, with_volume=False)
    assert fires_on_last(make(mirror(rows)), LOOSE, MINUTE, rising=False, with_volume=False)
    assert not fires_on_last(make(rows)[1:], LOOSE, MINUTE, rising=True, with_volume=False)
