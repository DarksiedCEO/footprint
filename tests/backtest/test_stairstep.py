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
# Until that close the steps still count (G39-1): bar 10 is push 4 (from bar 9) and
# bar 11 is pause 4 (low 111 > 109, gives back 2 of 4, volume 100 < 200), which bar 12
# ends as a valid step.
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
    ("113", "111", "112", 100),
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


# --- after the stair is formed: the steps still count until the entry close (G39-1) -----
# Bar 10 breaks push 3 (112) and closes 111: the stair is formed, no entry yet. Bar 10 is
# push 4's first new-high bar (from bar 9, low 109; push 4 high 113); bar 11 is pause 4.


def test_a_collapse_after_the_stair_forms_resets_it_before_the_entry_close() -> None:
    # Pause 4 falls below pause 3's low (109) and gives back far over 50% of push 4:
    # an invalid step resets the count, so the later close above 112 is not an entry.
    rows = [
        *STAIR[:11],
        ("111", "95", "96", 100),
        ("100", "90", "91", 100),
        ("105", "92", "104", 100),
        ("113", "104", "112.5", 200),
    ]
    assert up(make(rows)) == []
    assert down(make(mirror(rows))) == []


def test_a_drift_longer_than_max_pause_bars_after_the_stair_forms_cancels_it() -> None:
    drift = [("112.5", "111", "111.5", 100)] * 30
    rows = [*STAIR[:11], *drift, ("114", "111", "112.5", 200)]
    assert up(make(rows)) == []
    assert down(make(mirror(rows))) == []


def test_pause_four_at_the_exact_boundaries_keeps_or_cancels_the_stair() -> None:
    # Pause 4 is STAIR bar 11 (low 111 > 109; gives back 2 of push 4's 4: exactly 50%).
    assert up(make(STAIR)) == [ENTRY]
    # Pause 4 low equal to pause 3's low: not a higher low: reset, no entry.
    assert up(make(edit(STAIR, 11, lo="109"))) == []
    # With giveback allowed up to 100%, the equal low alone (a) still resets it, also
    # when the entry close comes on a later pause-4 bar (high 113, not above push 4).
    wide = StairSettings(steps=3, max_giveback=Decimal(1), max_pause_bars=10, max_stair_bars=100)
    late = ("113", "110", "112.5", 100)
    assert up(make([*STAIR[:11], ("113", "109.01", "111", 100), late]), wide) == [12]
    assert up(make([*STAIR[:11], ("113", "109", "111", 100), late]), wide) == []
    # Pause 4 gives back just over 50% (2.01 of 4) but stays above 109: reset.
    assert up(make(edit(STAIR, 11, lo="110.99"))) == []
    # A pause 4 of exactly max_pause_bars keeps the stair; one bar more cancels it.
    held = ("113", "111", "111.5", 100)
    two = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=2, max_stair_bars=100)
    rows = [*STAIR[:11], held, held, STAIR[12]]
    assert up(make(rows), two) == [13]
    rows = [*STAIR[:11], held, held, held, STAIR[12]]
    assert up(make(rows), two) == []


def test_vol_variant_judges_step_four_when_its_pause_ends_before_the_entry() -> None:
    # Pause 4 (bar 11) volume equal to push 4's average (bar 10: 200): step 4 is invalid
    # under _vol when bar 12 ends the pause, so bar 12's close is not an entry.
    rows = edit(STAIR, 11, v=200)
    assert up(make(rows), vol=True) == []
    assert up(make(rows)) == [ENTRY]
    assert down(make(mirror(rows)), vol=True) == []


# --- the core words: lowest low, averages over every bar (G39-2) -----------------------


def test_the_pause_low_is_the_lowest_low_in_the_pause_not_its_last_bar() -> None:
    # Pause 2 = two bars, the first the lower (104.9): gives back 3.1 of 6 (> 50%).
    rows = [*STAIR[:6], ("107", "104.9", "106", 100), ("107", "105.5", "106", 100), *STAIR[7:]]
    assert up(make(rows)) == []
    # The same with the first at 105.1: within 50%: entry.
    rows = [*STAIR[:6], ("107", "105.1", "106", 100), ("107", "105.5", "106", 100), *STAIR[7:]]
    assert up(make(rows)) == [ENTRY + 1]


def test_the_higher_low_test_uses_the_previous_pause_lowest_low() -> None:
    # Pause 1 = three bars, lows 102.2, 102.8, 102.3: its pause low is 102.2. Pause 2's
    # low 102.5 is above 102.2 (valid) though below the pause's highest low 102.8.
    wide = StairSettings(steps=3, max_giveback=Decimal(1), max_pause_bars=10, max_stair_bars=100)
    pause1 = [("103", "102.2", "102.5", 100), ("103", "102.8", "103", 100)]
    pause1 += [("103", "102.3", "102.5", 100)]
    rows = [*STAIR[:3], *pause1, *edit(STAIR, 6, lo="102.5")[4:]]
    assert up(make(rows), wide) == [ENTRY + 2]
    rows = [*STAIR[:3], *pause1, *edit(STAIR, 6, lo="102.2")[4:]]
    assert up(make(rows), wide) == []


def test_vol_averages_weigh_every_push_and_pause_bar_once() -> None:
    # Push 2 = bars 4 (120) and 5 (70): average 95, not above pause 2's 100: invalid.
    # (Counting only its first bar, or its first bar twice, would make it valid.)
    rows = edit(edit(STAIR, 4, v=120), 5, v=70)
    assert up(make(rows), vol=True) == []
    rows = edit(edit(STAIR, 4, v=130), 5, v=80)  # average 105: valid
    assert up(make(rows), vol=True) == [ENTRY]
    # Pause 2 = bars at 100 and 400: average 250, not below push 2's 200: invalid.
    rows = [*STAIR[:7], ("107", "105.5", "106", 400), *STAIR[7:]]
    assert up(make(rows), vol=True) == []
    rows = [*STAIR[:7], ("107", "105.5", "106", 250), *STAIR[7:]]  # average 175: valid
    assert up(make(rows), vol=True) == [ENTRY + 1]


# --- after an over-long pause or stair the push/pause partition goes on (G40-1) ---------
# "A PAUSE is the bars after a push until a bar makes a new high above that push's high":
# a pause that runs past max_pause_bars, or a stair that runs past max_stair_bars, makes
# the step invalid (count zero, reading Q5.2-c), but those bars are still that pause, so
# no new push starts before a bar exceeds the old push high (as for (a)/(b) failures).

# Push 1 to 140 (start bar 0, low 98), then pause bars 3-6 at 120-125 (never above 140).
LONG_PAUSE_HEAD = [
    ("100", "98", "99", 50),
    ("110", "99", "109", 200),
    ("140", "109", "139", 200),
    ("125", "120", "121", 100),
    ("124", "120", "121", 100),
    ("124", "120", "121", 100),
    ("124", "120", "121", 100),
]
# A stair whose highs all stay below 140: pushes to 130, 134, 138 with pause lows 127,
# 131, 135 (each higher, each giving back at most 50%), closing 138.5 on its last bar.
MINI_STAIR = [
    ("126", "121", "125", 200),
    ("130", "124", "129", 200),
    ("129", "127", "128", 100),
    ("132", "127.5", "131", 200),
    ("134", "130", "133", 200),
    ("133", "131", "132", 100),
    ("136", "131.5", "135", 200),
    ("138", "135", "137", 200),
    ("137", "135", "136", 100),
    ("139", "135.5", "138.5", 200),
]
# After the old pause: a bar above 140 starts push 1 of a new count (from the last pause
# bar, low 120); three valid steps follow and bar 13 closes above push 3's 149: ENTRY.
NEW_STAIR = [
    ("141", "121", "140", 200),
    ("140", "135", "136", 100),
    ("145", "136", "144", 200),
    ("144", "141", "142", 100),
    ("149", "142", "148", 200),
    ("148", "146", "147", 100),
    ("150", "147", "149.5", 200),
]


def test_no_stair_forms_inside_a_pause_that_ran_past_max_pause_bars() -> None:
    three = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=3, max_stair_bars=100
    )
    rows = [*LONG_PAUSE_HEAD, *MINI_STAIR]
    assert up(make(rows), three) == []
    assert up(make(rows), three, vol=True) == []
    assert down(make(mirror(rows)), three) == []
    assert down(make(mirror(rows)), three, vol=True) == []
    # The same stair on its own (one lead bar) does fire: only the old pause blocks it.
    assert up(make([("121", "120", "120", 100), *MINI_STAIR]), three) == [10]


def test_after_an_over_long_pause_the_bar_above_the_old_push_high_starts_a_new_count() -> None:
    three = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=3, max_stair_bars=100
    )
    rows = [*LONG_PAUSE_HEAD, *NEW_STAIR]
    assert up(make(rows), three) == [13]
    assert up(make(rows), three, vol=True) == [13]
    assert down(make(mirror(rows)), three) == [13]


def test_no_stair_forms_inside_a_pause_once_the_stair_ran_past_max_stair_bars() -> None:
    # Pause 1 is 12 bars (3-14), so max_pause 20 never bites; the old stair (from bar 0)
    # passes 12 bars on bar 12, inside the pause. The mini stair (start bar 14 to its
    # close on bar 24: 11 bars) would fit in 12 bars on its own.
    pause = [("124", "120", "121", 100)] * 8
    rows = [*LONG_PAUSE_HEAD, *pause, *MINI_STAIR]
    twelve = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=20, max_stair_bars=12
    )
    assert up(make(rows), twelve) == []
    assert down(make(mirror(rows)), twelve) == []
    assert up(make([("121", "120", "120", 100), *MINI_STAIR]), twelve) == [10]


# --- the bar that breaks pause 4 and closes above the level is not the entry (G40-2) ----


def test_a_pause_bar_that_breaks_the_step_and_closes_above_the_level_is_not_the_entry() -> None:
    # Bar 11 (pause 4) closes 112.5 above push 3's 112 but its low breaks the step:
    # (a) low 108 is not above pause 3's 109; (b) low 110.5 gives back 2.5 of push 4's 4.
    later = ("113", "111", "112.5", 100)
    for low in ("108", "110.5"):
        rows = [*STAIR[:11], ("113", low, "112.5", 100), later]
        assert up(make(rows)) == []
        assert down(make(mirror(rows))) == []
    # With a low that keeps the step valid, that same bar is the entry.
    rows = [*STAIR[:11], ("113", "111", "112.5", 100)]
    assert up(make(rows)) == [11]
    assert down(make(mirror(rows))) == [11]


# --- what starts the next count after an invalid step (review G41) ----------------------


def test_no_stair_forms_inside_pause_4_after_it_breaks_the_step() -> None:
    # G41-1: bar 11 (pause 4) breaks (a) and (b) after the stair forms (low 100). The bars
    # after it stay below push 4's high 113, so they are still pause 4: the stair they
    # draw (pushes to 104, 106, 108; pause lows 102, 104, 106; bar 18 closes 108.5 above
    # 108) is not a stair. Only a bar above 113 can start the next count.
    rows = [
        *STAIR[:11],
        ("101", "100", "100.5", 100),
        ("104", "100.5", "103.5", 200),
        ("103", "102", "102.5", 100),
        ("106", "102.5", "105.5", 200),
        ("105", "104", "104.5", 100),
        ("108", "104.5", "107.5", 200),
        ("107", "106", "106.5", 100),
        ("109", "106.5", "108.5", 200),
    ]
    assert up(make(rows)) == []
    assert up(make(rows), vol=True) == []
    assert down(make(mirror(rows))) == []
    assert down(make(mirror(rows)), vol=True) == []
    # The same mini stair on its own (one lead bar) does fire: only pause 4 blocks it.
    assert up(make([("101", "100", "100.5", 100), *rows[12:]])) == [7]


# Push 1 starts from bar 0 (low 90) and rises on bars 1-9 (highs 101..109). Then four
# clean steps: a pause bar giving back 1, and a push bar 3 higher.
LONG_PUSH = [
    ("100", "90", "99", 50),
    *[(str(100 + k), str(98 + k), f"{99 + k}.5", 200) for k in range(1, 10)],
    ("108.5", "108", "108.2", 100),
    ("112", "108.1", "111.5", 200),
    ("111.5", "111", "111.2", 100),
    ("115", "111.1", "114.5", 200),
    ("114.5", "114", "114.2", 100),
    ("118", "114.1", "117.5", 200),
    ("117.5", "117", "117.2", 100),
    ("121", "117.1", "120.5", 200),
]


def test_a_stair_that_passes_max_stair_bars_mid_push_does_not_count_that_push() -> None:
    # G41-2, reading Q5.2-c: with max_stair_bars 8 the stair (from bar 0) passes 8 bars
    # on bar 8 while push 1 is still rising. That push and its pause (bar 10) are not
    # counted; bar 11 (above 109) starts the new count from bar 10: steps end on bars
    # 13, 15, 17, and bar 17 closes 120.5 above push 3's 118 (stair bars 10-17: 8).
    eight = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=8)
    assert up(make(LONG_PUSH), eight) == [17]
    assert down(make(mirror(LONG_PUSH)), eight) == [17]
    # Without the limit, push 1 is step 1 and bar 15 closes above push 3's 115.
    assert up(make(LONG_PUSH)) == [15]
    assert down(make(mirror(LONG_PUSH))) == [15]


def test_a_push_bar_past_max_stair_bars_is_not_the_entry() -> None:
    # G42-1: the stair forms on bar 10 (level 112) and push 4 keeps rising; bar 11 is a
    # push bar (no pause yet) that closes 112.5 above the level. The stair runs from bar
    # 0 to the entry bar inclusive (Q5.2-a(7)): 12 bars. With max_stair_bars 12 bar 11 is
    # the entry; with 11 the stair is too long on that very bar and nothing fires.
    rows = [*STAIR[:10], ("113", "110", "111", 200), ("114", "112", "112.5", 200)]
    fits = StairSettings(steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=12)
    short = StairSettings(
        steps=3, max_giveback=Decimal("0.5"), max_pause_bars=10, max_stair_bars=11
    )
    assert up(make(rows), fits) == [11]
    assert up(make(rows), fits, vol=True) == [11]
    assert down(make(mirror(rows)), fits) == [11]
    assert up(make(rows), short) == []
    assert up(make(rows), short, vol=True) == []
    assert down(make(mirror(rows)), short) == []
    assert down(make(mirror(rows)), short, vol=True) == []
