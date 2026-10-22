from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from footprint.core.black76 import Right76, price
from footprint.research.bars import Day
from footprint.research.engine import DayState, Signal, Trade, run
from footprint.research.settings import ResearchSettings
from footprint.research.vol import VolIndex
from tests.research.conftest import MINUTE, NY, make_day, ny

VOL = VolIndex((date(2024, 3, 4),), (20.0,), divisor=100.0, max_age_days=5)


class OneShot:
    """Trivial test rule: enter `direction` on the close of bar `at` (if flat and no trade
    yet), optional futures stop, flat at `flat`."""

    name = "one_shot"

    def __init__(
        self,
        at: int,
        direction: int = 1,
        stop: float | None = None,
        flat: time = time(8, 25),
        again: bool = False,
    ) -> None:
        self.at = at
        self.direction = direction
        self.stop = stop
        self.flat = flat
        self.again = again
        self.seen: list[tuple[int, DayState]] = []

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None:
        self.seen.append((i, state))
        if (state.trades and not self.again) or i < self.at:
            return None
        flat_by = datetime.combine(day.day, self.flat, tzinfo=NY)
        return Signal(self.direction, flat_by, self.stop)


def _settings(research: ResearchSettings) -> ResearchSettings:
    return replace(research, option=replace(research.option, reference_price=None))


def _value(right: Right76, f: float, k: float, years: float) -> float:
    return price(right, forward=f, strike=k, years=years, vol=0.2, rate=0.0)


def test_entry_at_the_trigger_close_and_flat_at_the_hard_time(research: ResearchSettings) -> None:
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 8, 20), [18000, 18010, 18020, 18030, 18040, 18050, 18060])
    trades, skipped = run([day], OneShot(at=1), VOL, s, "booker")
    assert skipped == 0
    (t,) = trades
    assert t.session == "london"  # the entry bar (08:21) is in London
    assert t.direction == 1
    assert t.contracts == 1
    assert t.entry_ts == ny(2024, 3, 5, 8, 22)  # the close of bar 1
    assert t.exit_ts == ny(2024, 3, 5, 8, 25)  # the open of the 08:25 bar
    assert t.reasons == ("flat",)
    y = 1 / 365
    entry = _value(Right76.CALL, 18010, 18000, y)
    out = _value(Right76.CALL, 18040, 18000, y - 3 / (24 * 60 * 365))
    assert t.entry_value == pytest.approx(entry)
    assert t.net == pytest.approx((out - entry) * 20 - 170)


def test_put_on_direction_minus_one(research: ResearchSettings) -> None:
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 8, 20), [18000, 18010, 18000, 17990, 17980, 17970])
    (t,) = run([day], OneShot(at=1, direction=-1), VOL, s, "booker")[0]
    assert t.direction == -1
    entry = _value(Right76.PUT, 18010, 18000, 1 / 365)
    assert t.entry_value == pytest.approx(entry)
    assert t.net > 0


def test_flat_at_end_of_day_when_no_bar_reaches_the_flat_time(research: ResearchSettings) -> None:
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 8, 0), [18000, 18010, 18020, 18025])
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    assert t.reasons == ("end_of_day",)
    assert t.exit_ts == ny(2024, 3, 5, 8, 4)  # the last bar's close


def test_flat_on_a_gap_bar_after_the_flat_time(research: ResearchSettings) -> None:
    s = _settings(research)
    first = make_day(ny(2024, 3, 5, 8, 20), [18000, 18010])
    later = make_day(ny(2024, 3, 5, 8, 30), [18050, 18060])
    day = Day(
        first.day,
        first.ts + later.ts,
        first.clock + later.clock,
        first.open + later.open,
        first.high + later.high,
        first.low + later.low,
        first.close + later.close,
        first.volume + later.volume,
    )
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    assert t.reasons == ("flat",)
    assert t.exit_ts == ny(2024, 3, 5, 8, 30)
    entry = _value(Right76.CALL, 18000, 18000, 1 / 365)
    out = _value(Right76.CALL, 18050, 18000, 1 / 365 - 9 / (24 * 60 * 365))
    assert t.net == pytest.approx((out - entry) * 20 - 170)


def test_adverse_extreme_comes_first_inside_a_bar(research: ResearchSettings) -> None:
    """A bar whose low hits the stop and whose high would reach the lock stops out."""
    s = _settings(research)
    closes = [18000, 18000, 18000]
    day = make_day(
        ny(2024, 3, 5, 4, 0), closes, highs=[18000, 18000, 18400], lows=[18000, 18000, 17700]
    )
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    assert t.reasons == ("stop",)
    assert t.net == pytest.approx(-750 - 170)
    put_day = make_day(
        ny(2024, 3, 5, 4, 0), closes, highs=[18000, 18000, 18300], lows=[18000, 18000, 17600]
    )
    (p,) = run([put_day], OneShot(at=0, direction=-1), VOL, s, "booker")[0]
    assert p.reasons == ("stop",)
    assert p.net == pytest.approx(-750 - 170)


def test_bar_open_through_the_stop_fills_at_the_open(research: ResearchSettings) -> None:
    s = _settings(research)
    day = make_day(
        ny(2024, 3, 5, 4, 0),
        [18000, 18000, 17700],
        opens=[18000, 18000, 17700],
        highs=[18000, 18000, 17700],
        lows=[18000, 18000, 17700],
    )
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    entry = _value(Right76.CALL, 18000, 18000, 1 / 365)
    gap = _value(Right76.CALL, 17700, 18000, 1 / 365 - 1 / (24 * 60 * 365))
    assert t.reasons == ("stop",)
    assert t.net == pytest.approx((gap - entry) * 20 - 170)
    assert t.net < -750 - 170


def test_futures_stop_from_the_rule(research: ResearchSettings) -> None:
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 4, 0), [18000, 17990, 17980], lows=[18000, 17985, 17975])
    (t,) = run([day], OneShot(at=0, stop=17990), VOL, s, "booker")[0]
    assert t.reasons == ("futures_stop",)
    entry = _value(Right76.CALL, 18000, 18000, 1 / 365)
    at_stop = _value(Right76.CALL, 17990, 18000, 1 / 365 - 1 / (24 * 60 * 365))
    assert t.net == pytest.approx((at_stop - entry) * 20 - 170)


def test_scale_out_records_both_legs(research: ResearchSettings) -> None:
    s = _settings(research)
    up = [18000 + 40 * k for k in range(8)] + [18100]
    day = make_day(ny(2024, 3, 5, 4, 0), up)
    (t,) = run([day], OneShot(at=0), VOL, s, "scale_out")[0]
    assert t.contracts == 2
    assert t.reasons[0] == "target"
    assert len(t.reasons) == 2


def test_one_position_at_a_time_and_next_entry_after_the_exit_bar(
    research: ResearchSettings,
) -> None:
    s = _settings(research)
    day = make_day(
        ny(2024, 3, 5, 4, 0), [18000] * 6, lows=[18000, 17700, 18000, 18000, 18000, 18000]
    )
    rule = OneShot(at=0, again=True)
    trades, _ = run([day], rule, VOL, s, "booker")
    assert [t.entry_ts for t in trades] == [ny(2024, 3, 5, 4, 1), ny(2024, 3, 5, 4, 3)]
    # The rule is not asked while a trade is open: not on bar 1 (the first trade's exit
    # bar) and not after the second entry (bar 2), which stays open to the end.
    asked = [i for i, _ in rule.seen]
    assert asked == [0, 2]
    states = dict(rule.seen)
    assert states[2].trades == 1
    assert states[2].realized == pytest.approx(trades[0].net)
    assert states[0].trades == 0
    assert states[0].realized == 0


def test_state_resets_each_day(research: ResearchSettings) -> None:
    s = _settings(research)
    d1 = make_day(ny(2024, 3, 5, 4, 0), [18000, 18010, 18020])
    d2 = make_day(ny(2024, 3, 6, 4, 0), [18000, 18010, 18020])
    trades, _ = run([d1, d2], OneShot(at=0), VOL, s, "booker")
    assert len(trades) == 2
    assert [t.day for t in trades] == [date(2024, 3, 5), date(2024, 3, 6)]


def test_day_without_usable_vol_is_skipped_and_counted(research: ResearchSettings) -> None:
    s = _settings(research)
    d_ok = make_day(ny(2024, 3, 5, 4, 0), [18000, 18010])
    d_stale = make_day(ny(2024, 3, 20, 4, 0), [18000, 18010])
    d_early = make_day(ny(2024, 3, 1, 4, 0), [18000, 18010])
    trades, skipped = run([d_early, d_ok, d_stale], OneShot(at=0), VOL, s, "booker")
    assert [t.day for t in trades] == [date(2024, 3, 5)]
    assert skipped == 2


def test_day_with_no_signal_is_not_counted_as_skipped(research: ResearchSettings) -> None:
    s = _settings(research)
    d = make_day(ny(2024, 3, 20, 4, 0), [18000, 18010])
    assert run([d], OneShot(at=5), VOL, s, "booker") == ([], 0)


def test_entry_on_the_last_bar_is_valued_and_flattened_at_once(
    research: ResearchSettings,
) -> None:
    s = _settings(research)
    d = make_day(ny(2024, 3, 5, 4, 0), [18000, 18010])
    (t,) = run([d], OneShot(at=1), VOL, s, "booker")[0]
    assert t.reasons == ("end_of_day",)
    assert t.net == pytest.approx(-170)


def test_signal_at_or_after_flat_time_is_refused(research: ResearchSettings) -> None:
    s = _settings(research)
    d = make_day(ny(2024, 3, 5, 8, 23), [18000, 18010, 18020, 18030])
    trades, _ = run([d], OneShot(at=1), VOL, s, "booker")  # entry 08:25 = flat time
    assert trades == []


def test_reference_price_rescales_dollar_results(research: ResearchSettings) -> None:
    base = _settings(research)
    scaled = replace(base, option=replace(base.option, reference_price=36000.0))
    day = make_day(ny(2024, 3, 5, 4, 0), [18000, 18020, 18040])
    (a,) = run([day], OneShot(at=0), VOL, base, "booker")[0]
    (b,) = run([day], OneShot(at=0), VOL, scaled, "booker")[0]
    assert b.net + 170 == pytest.approx((a.net + 170) * 2)


def test_trade_is_a_plain_record() -> None:
    t = Trade(
        date(2024, 3, 5),
        "london",
        1,
        ny(2024, 3, 5, 4, 0),
        ny(2024, 3, 5, 4, 1),
        1,
        10.0,
        -5.0,
        ("flat",),
    )
    assert t.net == -5.0
    assert t.exit_ts - t.entry_ts == MINUTE
    assert isinstance(t.exit_ts - t.entry_ts, timedelta)


# --- review round 1 (37-M1, 37-L1, 37-L3, 37-M3) ----------------------------------------


def test_the_vol_of_the_day_before_prices_the_trade(research: ResearchSettings) -> None:
    """37-M1: the engine asks for the session day's vol, which is the close dated
    strictly before it (never the session day's own close, never a later one)."""
    s = _settings(research)
    vols = VolIndex(
        (date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)),
        (20.0, 40.0, 80.0),
        divisor=100.0,
        max_age_days=5,
    )
    day = make_day(ny(2024, 3, 5, 4, 0), [18000, 18010, 18020])
    (t,) = run([day], OneShot(at=0), vols, s, "booker")[0]
    assert t.entry_value == pytest.approx(_value(Right76.CALL, 18000, 18000, 1 / 365))


def test_green_stop_input_sums_every_closed_trade(research: ResearchSettings) -> None:
    """37-L1: realized is the running SUM of the day's closed trades (three here)."""
    s = _settings(research)
    lows = [18000, 17700, 18000, 17700, 18000, 18000, 18000]
    day = make_day(ny(2024, 3, 5, 4, 0), [18000] * 7, lows=lows)
    rule = OneShot(at=0, again=True)
    trades, _ = run([day], rule, VOL, s, "booker")
    assert len(trades) == 3
    states = dict(rule.seen)
    assert states[2].trades == 1
    assert states[4].trades == 2
    assert states[4].realized == pytest.approx(trades[0].net + trades[1].net)
    assert trades[0].net != 0


def test_a_futures_stop_at_the_entry_price_is_refused(research: ResearchSettings) -> None:
    """37-L3: a stop must be strictly on the losing side of the entry."""
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 4, 0), [18000, 18010, 18020])
    with pytest.raises(ValueError, match="losing side"):
        run([day], OneShot(at=0, stop=18000), VOL, s, "booker")
    with pytest.raises(ValueError, match="losing side"):
        run([day], OneShot(at=0, direction=-1, stop=18000), VOL, s, "booker")
    with pytest.raises(ValueError, match="losing side"):
        run([day], OneShot(at=0, stop=18001), VOL, s, "booker")


def test_marks_follow_every_path_point(research: ResearchSettings) -> None:
    """37-M3: the trade's open profit, marked at every path point (entry first, the
    final net last), so drawdown inside a trade counts."""
    s = _settings(research)
    day = make_day(
        ny(2024, 3, 5, 4, 0),
        [18000, 18000, 18010],
        opens=[18000, 18000, 18000],
        highs=[18000, 18000, 18010],
        lows=[18000, 17950, 18000],
    )
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    # entry, bar 1 (open, low, high, close), bar 2 (open, low, high, close), end of day
    assert len(t.marks) == 1 + 4 + 4 + 1
    assert t.marks[0] == pytest.approx(-170)
    assert t.marks[-1] == pytest.approx(t.net)
    entry = _value(Right76.CALL, 18000, 18000, 1 / 365)
    low = _value(Right76.CALL, 17950, 18000, 1 / 365 - 1 / (24 * 60 * 365))
    assert t.marks[2] == pytest.approx((low - entry) * 20 - 170)
    assert min(t.marks) == pytest.approx(t.marks[2])
    assert min(t.marks) < t.net


def test_marks_after_a_scale_out_sale_keep_the_sold_contract(
    research: ResearchSettings,
) -> None:
    s = _settings(research)
    up = [18000 + 40 * k for k in range(8)] + [18100]
    (t,) = run([make_day(ny(2024, 3, 5, 4, 0), up)], OneShot(at=0), VOL, s, "scale_out")[0]
    assert t.marks[0] == pytest.approx(-2 * 170)
    assert t.marks[-1] == pytest.approx(t.net)


def test_no_mark_is_recorded_at_a_fill_point(research: ResearchSettings) -> None:
    """37-R2-L1: marks are taken at the path points with no fill; the trade's final net
    is appended once. A trade stopped at its first bar's low has three marks: entry,
    that bar's open, and the final net (no extra mark at the stop itself)."""
    s = _settings(research)
    day = make_day(ny(2024, 3, 5, 4, 0), [18000, 18000], lows=[18000, 17700])
    (t,) = run([day], OneShot(at=0), VOL, s, "booker")[0]
    assert t.reasons == ("stop",)
    assert len(t.marks) == 3
    assert t.marks[0] == pytest.approx(-170)
    assert t.marks[1] == pytest.approx(-170)  # the open, unchanged from the entry
    assert t.marks[2] == pytest.approx(t.net)
