"""Q5.2 entry "stairstep" / "stairstep_vol": the stair detector, on hand-built 1-min bars.

Andre's approved definition (2026-10-10) is in docs/strategy-rules.md word for word;
how each ambiguous word is read is recorded in docs/decisions-pending.md (Q5.2-a).

These fixtures prove the detector does what the definition says, bar by bar. They
say NOTHING about profitability.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from footprint.backtest.stairstep import StairSettings, stair_entries
from footprint.core.models import Bar

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)  # a Wednesday

# Steps 3 and giveback 0.5 are Andre's approved values; the two maxima are loose here
# so that only the condition under test can block the stair.
LOOSE = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=100)

# (high, low, close, volume). Open is set to the low (it plays no part in the rule).
# Push 1: start bar 0 (low 98), new highs on 1, 2 (push high 104).
# Pause 1: bar 3 (low 102): gives back 2 of 6 (<= 3), first step, no higher-low test.
# Push 2: starts from bar 3 (low 102), new highs on 4, 5 (push high 108).
# Pause 2: bar 6 (low 105): gives back 3 of 6, EXACTLY 50%; 105 > 102.
# Push 3: starts from bar 6 (low 105), new highs on 7, 8 (push high 112).
# Pause 3: bar 9 (low 109): gives back 3 of 7; 109 > 105.
# Bar 10 breaks 112 (step 3 valid, the stair is formed) but closes 111.
# Bar 11 closes exactly 112: not above. Bar 12 closes 112.5: ENTRY.
STAIR = [
    ("100", "98", "99", 50),
    ("102", "99", "101", 200),
    ("104", "101", "103", 200),
    ("103", "102", "102", 100),
    ("106", "102", "105", 200),
    ("108", "105", "107", 200),
    ("107", "105", "106", 100),
    ("110", "106", "109", 200),
    ("112", "108", "111", 200),
    ("111", "109", "110", 100),
    ("113", "110", "111", 200),
    ("113", "111", "112", 200),
    ("114", "111", "112.5", 200),
]
ENTRY = 12


def make(rows: list[tuple[str, str, str, int]], start: datetime = T0) -> list[Bar]:
    return [
        Bar("NQ", start + i * MINUTE, Decimal(lo), Decimal(hi), Decimal(lo), Decimal(c), v)
        for i, (hi, lo, c, v) in enumerate(rows)
    ]


def mirror(rows: list[tuple[str, str, str, int]]) -> list[tuple[str, str, str, int]]:
    """The exact down-move mirror: highs <-> lows, prices reflected about 0 (+1000)."""
    base = Decimal(1000)
    return [
        (str(base - Decimal(lo)), str(base - Decimal(hi)), str(base - Decimal(c)), v)
        for hi, lo, c, v in rows
    ]


def up(bars: list[Bar], settings: StairSettings = LOOSE, *, vol: bool = False) -> list[int]:
    return stair_entries(bars, settings, MINUTE, rising=True, with_volume=vol)


def down(bars: list[Bar], settings: StairSettings = LOOSE, *, vol: bool = False) -> list[int]:
    return stair_entries(bars, settings, MINUTE, rising=False, with_volume=vol)


def edit(
    rows: list[tuple[str, str, str, int]], i: int, **kw: object
) -> list[tuple[str, str, str, int]]:
    hi, lo, c, v = rows[i]
    new = (
        str(kw.get("hi", hi)),
        str(kw.get("lo", lo)),
        str(kw.get("c", c)),
        int(str(kw.get("v", v))),
    )
    return [*rows[:i], new, *rows[i + 1 :]]


# --- where it fires ------------------------------------------------------------------


def test_fires_exactly_on_the_first_bar_that_closes_above_the_third_push_high() -> None:
    assert up(make(STAIR)) == [ENTRY]
    # Every prefix: nothing before the entry bar (bar 10 breaks the high but closes
    # below; bar 11 closes exactly at it, which is not above).
    for end in range(1, ENTRY + 1):
        assert up(make(STAIR[:end])) == []


def test_only_the_first_close_above_is_the_entry() -> None:
    rows = [*STAIR, ("115", "112", "114", 200), ("116", "113", "115", 200)]
    assert up(make(rows)) == [ENTRY]


def test_the_breaking_bar_itself_is_the_entry_when_it_closes_above() -> None:
    rows = edit(STAIR[: ENTRY - 1], 10, c="112.25")
    assert up(make(rows)) == [10]


def test_no_volume_condition_without_the_vol_variant() -> None:
    flat = [(hi, lo, c, 100) for hi, lo, c, _ in STAIR]
    assert up(make(flat)) == [ENTRY]


def test_a_breaking_bar_that_closes_exactly_at_the_push_high_is_not_the_entry() -> None:
    assert up(make(edit(STAIR[: ENTRY - 1], 10, c="112"))) == []


def test_an_equal_high_is_not_a_new_high_inside_a_push() -> None:
    # Bar 3's high equals push 1's high (104): it is a pause bar, not a push bar.
    assert up(make(edit(STAIR, 3, hi="104"))) == [ENTRY]


def test_a_pause_bar_whose_high_equals_the_push_high_stays_in_the_pause() -> None:
    # Pause 2 = bar 6 and a second bar at 108 (= push 2's high) with low 104.5: the
    # pause gives back 3.5 of 6 (> 50%), so step 2 is invalid and nothing fires.
    rows = [*STAIR[:7], ("108", "104.5", "106", 100), *STAIR[7:]]
    assert up(make(rows)) == []
    # The same second bar with low 105.5 keeps the pause within 50%: entry.
    rows = [*STAIR[:7], ("108", "105.5", "106", 100), *STAIR[7:]]
    assert up(make(rows)) == [ENTRY + 1]


# --- (b) giveback ----------------------------------------------------------------------


def test_exactly_fifty_percent_giveback_qualifies_and_just_over_does_not() -> None:
    assert up(make(STAIR)) == [ENTRY]  # pause 2 gives back exactly 3 of 6
    over = edit(STAIR, 6, lo="104.99")  # 3.01 of 6
    assert up(make(over)) == []


def test_giveback_is_measured_from_the_low_of_the_bar_the_push_started_from() -> None:
    # Pause 1 low 101 gives back 3 of push 1 (104 - 98 = 6): exactly 50%, valid.
    assert up(make(edit(STAIR, 3, lo="101"))) == [ENTRY]
    # Start bar low 98.01: push is 5.99, half is 2.995 < 3: invalid.
    rows = edit(edit(STAIR, 3, lo="101"), 0, lo="98.01")
    assert up(make(rows)) == []


# --- (a) higher low ----------------------------------------------------------------------


def test_pause_low_must_be_strictly_above_the_previous_pause_low() -> None:
    wide = StairSettings(steps=3, max_giveback=Decimal(1), max_pause_bars=10, max_stair_bars=100)
    assert up(make(edit(STAIR, 6, lo="102.01")), wide) == [ENTRY]
    assert up(make(edit(STAIR, 6, lo="102")), wide) == []  # equal is not above


# --- resets ------------------------------------------------------------------------------


def test_an_invalid_step_resets_the_count_and_a_new_stair_can_follow() -> None:
    # Make step 2 invalid (giveback over 50%): the count resets at bar 7; push 3 then
    # starts a new stair as its step 1, so only two valid steps exist: no entry ...
    bad = edit(STAIR, 6, lo="104.99")
    assert up(make(bad)) == []
    # ... until it has three. After bar 12 the count is 2 (step at bar 10: push 3; step
    # at bar 12: push 109 -> 113, pause low 111), and push 5 runs from bar 11 (low 111).
    more = [
        ("116", "113", "115", 200),  # 13: push 5 high 116
        ("115", "114", "114.5", 100),  # 14: pause 5, low 114 > 111; gives back 2 of 5
        ("117", "114.5", "116", 200),  # 15: breaks 116: three valid steps; closes 116
        ("118", "115", "117", 200),  # 16: closes above 116: ENTRY
    ]
    assert up(make([*bad, *more])) == [16]


def test_after_an_entry_the_count_starts_again_from_zero() -> None:
    # After the entry one more valid step (push from bar 12 to 115, pause low 113.5)
    # and a close above 115: a count that carried over would fire again on bar 15.
    rows = [*STAIR, ("115", "112", "114", 200), ("114", "113.5", "114", 100)]
    rows += [("116", "114", "115.5", 200)]
    assert up(make(rows)) == [ENTRY]


# --- the two config DEFAULTS -------------------------------------------------------------


def test_max_pause_bars() -> None:
    # Pause 2 lasts two bars (6 and an extra one).
    longer = [*STAIR[:7], ("107", "105.5", "106", 100), *STAIR[7:]]
    entry = ENTRY + 1
    two = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=2, max_stair_bars=100)
    one = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=1, max_stair_bars=100)
    assert up(make(longer), two) == [entry]
    assert up(make(longer), one) == []
    assert up(make(STAIR), one) == [ENTRY]


def test_max_stair_bars_counts_from_the_first_push_start_bar_to_the_entry_bar() -> None:
    span = ENTRY + 1  # bars 0..12
    fits = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=span
    )
    short = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=span - 1
    )
    assert up(make(STAIR), fits) == [ENTRY]
    assert up(make(STAIR), short) == []


def test_the_stair_starts_at_the_bar_before_the_first_new_high() -> None:
    # A leading bar with the same high as STAIR bar 0 (100): bar 0's equal high is not
    # a new high, so the stair starts at bar 0 (13 bars to the entry), not at the lead
    # bar (14 bars).
    rows = [("100", "98", "99", 50), *STAIR]
    span = ENTRY + 1
    fits = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=span
    )
    assert up(make(rows), fits) == [ENTRY + 1]


# --- volume variant ----------------------------------------------------------------------


def test_vol_variant_needs_each_push_average_volume_above_its_pause_average() -> None:
    assert up(make(STAIR), vol=True) == [ENTRY]
    # Pause 2 volume equal to push 2's average (200): not greater: invalid under _vol.
    rows = edit(STAIR, 6, v=200)
    assert up(make(rows), vol=True) == []
    assert up(make(rows)) == [ENTRY]
    # A push's average counts its new-high bars only, not the bar it started from
    # (bar 3, a pause-1 bar): with bar 3 at 50, push 2 averages 101 (> 100); counting
    # bar 3 it would average 84 (< 100).
    rows = edit(edit(edit(STAIR, 3, v=50), 4, v=101), 5, v=101)
    assert up(make(rows), vol=True) == [ENTRY]
    rows = edit(edit(STAIR, 4, v=100), 5, v=100)
    assert up(make(rows), vol=True) == []


# --- gaps, sessions, Saturdays -----------------------------------------------------------


@pytest.mark.parametrize("gap", [timedelta(minutes=1), timedelta(hours=1), timedelta(days=2)])
def test_a_missing_minute_breaks_the_stair(gap: timedelta) -> None:
    bars = make(STAIR)
    # Shift bars 7.. forward: a gap between pause 2 and push 3 (one missing minute, the
    # daily halt, or a weekend with no Saturday bars).
    shifted = [
        *bars[:7],
        *(Bar(b.symbol, b.ts + gap, b.open, b.high, b.low, b.close, b.volume) for b in bars[7:]),
    ]
    assert up(shifted) == []


def test_a_stair_after_a_gap_is_found_on_its_own() -> None:
    friday = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    sunday = datetime(2026, 10, 11, 22, 0, tzinfo=UTC)
    bars = [*make([("100", "90", "95", 10)] * 3, friday), *make(STAIR, sunday)]
    assert up(bars) == [3 + ENTRY]
    assert all(b.ts.weekday() != 5 for b in bars)  # no Saturday bars


# --- the down-move mirror ----------------------------------------------------------------


def test_the_down_move_is_the_exact_mirror() -> None:
    assert down(make(mirror(STAIR))) == [ENTRY]
    assert down(make(mirror(STAIR)), vol=True) == [ENTRY]
    assert up(make(mirror(STAIR))) == []
    assert down(make(STAIR)) == []
    over = edit(STAIR, 6, lo="104.99")
    assert down(make(mirror(over))) == []
    for end in range(1, ENTRY + 1):
        assert down(make(mirror(STAIR[:end]))) == []


def test_settings_must_be_sane() -> None:
    with pytest.raises(ValueError, match="steps "):
        StairSettings(steps=0, max_giveback=Decimal("0.5"), max_pause_bars=1, max_stair_bars=1)
    with pytest.raises(ValueError, match="max_giveback "):
        StairSettings(steps=3, max_giveback=Decimal(0), max_pause_bars=1, max_stair_bars=1)
    with pytest.raises(ValueError, match="max_pause_bars "):
        StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=0, max_stair_bars=1)
    with pytest.raises(ValueError, match="max_stair_bars "):
        StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=1, max_stair_bars=0)
