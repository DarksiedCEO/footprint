"""N2 (noiseband-es-v1): the `adr_bracket` exit, a futures stop and target at fractions of
the rule's average daily range; and the engine's next-bar-open entry it is used with."""

from dataclasses import replace
from datetime import date, datetime, time

import pytest

from footprint.core.black76 import Right76, price
from footprint.research.bars import Day
from footprint.research.engine import DayState, Signal, run
from footprint.research.exits import KINDS, Fill, OptionExit
from footprint.research.settings import AdrBracketSettings, ExitSettings, ResearchSettings
from footprint.research.vol import VolIndex
from tests.research.conftest import MINUTE, NY, make_day, ny

VOL = VolIndex((date(2024, 3, 4),), (20.0,), divisor=100.0, max_age_days=5)

EX = ExitSettings(
    stop_dollars=750.0,
    lock_start_dollars=1000.0,
    lock_fraction=0.5,
    first_target_dollars=1000.0,
    booker_contracts=1,
    scale_out_contracts=2,
    adr_bracket=AdrBracketSettings(
        contracts=1, stop_range_fraction=0.25, target_range_fraction=0.5
    ),
)


def F(contracts: int, value: float, reason: str) -> Fill:
    return Fill(contracts, pytest.approx(value), reason)  # type: ignore[arg-type]


def bracket(ex: ExitSettings = EX) -> OptionExit:
    return OptionExit("adr_bracket", 100.0, ex, point_value=50.0, cost=45.0)


# --- settings ---------------------------------------------------------------------------


def test_pre_registered_bracket_settings(research: ResearchSettings) -> None:
    assert "adr_bracket" in KINDS
    assert research.exits.adr_bracket == AdrBracketSettings(
        contracts=1, stop_range_fraction=0.25, target_range_fraction=0.5
    )


# --- the exit on its own ----------------------------------------------------------------


def test_contracts_from_config_and_bracket_required() -> None:
    assert bracket().contracts == 1
    two = replace(EX, adr_bracket=replace(EX.adr_bracket, contracts=2))  # type: ignore[type-var]
    assert bracket(two).contracts == 2
    with pytest.raises(ValueError, match="adr_bracket"):
        bracket(replace(EX, adr_bracket=None))
    with pytest.raises(ValueError, match="bracket"):
        bracket().check(100.0, gap=False)


def test_stop_fills_at_its_level_or_at_the_price_on_a_gap() -> None:
    e = bracket()
    assert e.check(95.0, gap=False, bracket=(90.0, 120.0)) == []
    assert e.check(89.0, gap=False, bracket=(90.0, 120.0)) == [F(1, 90.0, "range_stop")]
    assert e.done
    assert e.check(50.0, gap=False, bracket=(90.0, 120.0)) == []  # never sells twice
    g = bracket()
    assert g.check(85.0, gap=True, bracket=(90.0, 120.0)) == [F(1, 85.0, "range_stop")]
    at = bracket()
    assert at.check(90.0, gap=False, bracket=(90.0, 120.0)) == [F(1, 90.0, "range_stop")]


def test_target_fills_at_its_level_or_at_the_price_on_a_gap() -> None:
    e = bracket()
    assert e.check(119.0, gap=False, bracket=(90.0, 120.0)) == []
    assert e.check(121.0, gap=False, bracket=(90.0, 120.0)) == [F(1, 120.0, "range_target")]
    assert e.done
    g = bracket()
    assert g.check(125.0, gap=True, bracket=(90.0, 120.0)) == [F(1, 125.0, "range_target")]
    at = bracket()
    assert at.check(120.0, gap=False, bracket=(90.0, 120.0)) == [F(1, 120.0, "range_target")]


def test_target_sells_every_open_contract() -> None:
    two = replace(EX, adr_bracket=replace(EX.adr_bracket, contracts=2))  # type: ignore[type-var]
    e = bracket(two)
    assert e.check(121.0, gap=False, bracket=(90.0, 120.0)) == [F(2, 120.0, "range_target")]
    assert e.open == 0


def test_no_option_dollar_stop_or_lock() -> None:
    """The bracket is the whole exit: a value far below the booker stop but above the
    range stop, or a profit past the lock that falls back, sells nothing."""
    e = bracket()
    assert e.check(100.0 - 750.0 / 50.0 - 5.0, gap=False, bracket=(10.0, 500.0)) == []
    assert e.check(200.0, gap=False, bracket=(10.0, 500.0)) == []
    assert e.check(101.0, gap=False, bracket=(10.0, 500.0)) == []
    assert not e.done


def test_a_rule_futures_stop_still_competes() -> None:
    e = bracket()
    fills = e.check(94.0, gap=False, futures_level=95.0, bracket=(90.0, 120.0))
    assert fills == [F(1, 95.0, "futures_stop")]


def test_net_uses_point_value_and_cost() -> None:
    e = bracket()
    fills = e.check(121.0, gap=False, bracket=(90.0, 120.0))
    assert e.net(fills) == pytest.approx(20.0 * 50.0 - 45.0)


# --- through the engine -----------------------------------------------------------------


class NextOpen:
    """Enters `direction` at the open of the bar after bar `at`, with range `adr`."""

    name = "next_open"

    def __init__(self, at: int, direction: int = 1, adr: float | None = 40.0) -> None:
        self.at = at
        self.direction = direction
        self.adr = adr
        self.asked: list[int] = []

    def entry(self, day: Day, i: int, state: DayState) -> Signal | None:
        self.asked.append(i)
        if state.trades or i != self.at:
            return None
        flat_by = datetime.combine(day.day, time(15, 55), tzinfo=NY)
        return Signal(self.direction, flat_by, next_open=True, range_points=self.adr)


def _plain(research: ResearchSettings) -> ResearchSettings:
    return replace(research, option=replace(research.option, reference_price=None))


def _value(
    right: Right76, f: float, k: float, at: datetime, entry: datetime, s: ResearchSettings
) -> float:
    years = (s.option.days_to_expiry - (at - entry) / (MINUTE * 60 * 24)) / s.option.days_per_year
    return price(right, forward=f, strike=k, years=years, vol=0.2, rate=0.0)


def _day(opens: list[float], highs: list[float], lows: list[float], closes: list[float]) -> Day:
    return make_day(ny(2024, 3, 5, 10, 0), closes, opens=opens, highs=highs, lows=lows)


def test_next_open_entry_values_the_next_bar_open(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day(
        [18000, 18000, 18004, 18006],
        [18001, 18001, 18007, 18008],
        [17999, 17999, 18003, 18005],
        [18000, 18001, 18006, 18007],
    )
    rule = NextOpen(at=1)
    (t,) = run([day], rule, VOL, s, "booker")[0]
    assert t.entry_ts == ny(2024, 3, 5, 10, 2)  # the OPEN of bar 2
    k = round(18004 / s.option.strike_step) * s.option.strike_step
    assert t.entry_value == pytest.approx(_value(Right76.CALL, 18004, k, t.entry_ts, t.entry_ts, s))
    assert t.reasons == ("end_of_day",)
    assert rule.asked == [0, 1]  # not asked again while the trade is open
    # marks: the entry, the entry bar's extremes and close (its open IS the entry, not
    # walked again), the last bar's four points, the final net
    assert len(t.marks) == 1 + 3 + 4 + 1


def test_next_open_after_a_gap_enters_at_the_next_bar_time(research: ResearchSettings) -> None:
    s = _plain(research)
    a = make_day(ny(2024, 3, 5, 10, 0), [18000.0, 18001.0])
    b = make_day(ny(2024, 3, 5, 10, 5), [18003.0, 18004.0], opens=[18002.0, 18003.0])
    day = Day(
        a.day,
        a.ts + b.ts,
        a.clock + b.clock,
        a.open + b.open,
        a.high + b.high,
        a.low + b.low,
        a.close + b.close,
        a.volume + b.volume,
    )
    (t,) = run([day], NextOpen(at=1), VOL, s, "booker")[0]
    assert t.entry_ts == ny(2024, 3, 5, 10, 5)  # not the trigger's close at 10:02


class Prepared(NextOpen):
    def __init__(self) -> None:
        super().__init__(at=1)
        self.given: list[object] = []

    def prepare(self, days: list[Day]) -> None:
        self.given.append(list(days))


def test_run_calls_prepare_once_with_every_day(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day([18000] * 4, [18001] * 4, [17999] * 4, [18000] * 4)
    rule = Prepared()
    run([day, day], rule, VOL, s, "booker")
    assert rule.given == [[day, day]]


def test_the_entry_bar_itself_is_walked(research: ResearchSettings) -> None:
    """The entry bar's low after its open can stop the trade (the source rule checks the
    entry bar too): stop = 18004 - 0.25 * 40 = 17994."""
    s = _plain(research)
    day = _day(
        [18000, 18000, 18004, 18000],
        [18001, 18001, 18005, 18001],
        [17999, 17999, 17990, 17999],
        [18000, 18001, 18000, 18000],
    )
    (t,) = run([day], NextOpen(at=1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("range_stop",)
    assert t.exit_ts == ny(2024, 3, 5, 10, 3)  # the end of the entry bar
    k = round(18004 / s.option.strike_step) * s.option.strike_step
    entry = _value(Right76.CALL, 18004, k, t.entry_ts, t.entry_ts, s)
    stop = _value(Right76.CALL, 17994, k, t.exit_ts, t.entry_ts, s)
    assert t.net == pytest.approx((stop - entry) * s.option.point_value - s.round_trip_per_contract)


def test_target_at_half_the_range_for_a_call(research: ResearchSettings) -> None:
    s = _plain(research)
    # entry 18004; target 18004 + 0.5 * 40 = 18024 reached by bar 3's high
    day = _day(
        [18000, 18000, 18004, 18010, 18030],
        [18001, 18001, 18011, 18030, 18031],
        [17999, 17999, 18003, 18009, 18029],
        [18000, 18001, 18010, 18020, 18030],
    )
    (t,) = run([day], NextOpen(at=1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("range_target",)
    assert t.exit_ts == ny(2024, 3, 5, 10, 4)
    k = round(18004 / s.option.strike_step) * s.option.strike_step
    entry = _value(Right76.CALL, 18004, k, t.entry_ts, t.entry_ts, s)
    target = _value(Right76.CALL, 18024, k, t.exit_ts, t.entry_ts, s)
    assert t.net == pytest.approx(
        (target - entry) * s.option.point_value - s.round_trip_per_contract
    )


def test_put_levels_mirror(research: ResearchSettings) -> None:
    s = _plain(research)
    # put entry 17996; stop 17996 + 10 = 18006; target 17996 - 20 = 17976
    day = _day(
        [18000, 18000, 17996, 17990],
        [18001, 18001, 17997, 17991],
        [17999, 17999, 17995, 17975],
        [18000, 17999, 17995, 17980],
    )
    (t,) = run([day], NextOpen(at=1, direction=-1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("range_target",)
    k = round(17996 / s.option.strike_step) * s.option.strike_step
    entry = _value(Right76.PUT, 17996, k, t.entry_ts, t.entry_ts, s)
    target = _value(Right76.PUT, 17976, k, t.exit_ts, t.entry_ts, s)
    assert t.net == pytest.approx(
        (target - entry) * s.option.point_value - s.round_trip_per_contract
    )
    day = _day(
        [18000, 18000, 17996, 18000],
        [18001, 18001, 18007, 18001],
        [17999, 17999, 17995, 17999],
        [18000, 17999, 17999, 18000],
    )
    (t,) = run([day], NextOpen(at=1, direction=-1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("range_stop",)


def test_stop_before_target_inside_one_bar(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day(
        [18000, 18000, 18004, 18004],
        [18001, 18001, 18005, 18030],
        [17999, 17999, 18003, 17990],
        [18000, 18001, 18004, 18004],
    )
    (t,) = run([day], NextOpen(at=1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("range_stop",)


def test_flat_at_15_55_when_neither_level_is_reached(research: ResearchSettings) -> None:
    s = _plain(research)
    start = ny(2024, 3, 5, 15, 50)
    closes = [18000.0] * 9
    day = make_day(start, closes)
    (t,) = run([day], NextOpen(at=1), VOL, s, "adr_bracket")[0]
    assert t.reasons == ("flat",)
    assert t.exit_ts == ny(2024, 3, 5, 15, 55)


def test_bracket_without_a_range_is_an_error(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day([18000] * 4, [18001] * 4, [17999] * 4, [18000] * 4)
    with pytest.raises(ValueError, match="range"):
        run([day], NextOpen(at=1, adr=None), VOL, s, "adr_bracket")
    with pytest.raises(ValueError, match="range"):
        run([day], NextOpen(at=1, adr=0.0), VOL, s, "adr_bracket")


def test_booker_ignores_the_range(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day(
        [18000, 18000, 18004, 17000],
        [18001, 18001, 18005, 17001],
        [17999, 17999, 17990, 16999],
        [18000, 18001, 18000, 17000],
    )
    a = run([day], NextOpen(at=1), VOL, s, "booker")[0]
    b = run([day], NextOpen(at=1, adr=None), VOL, s, "booker")[0]
    assert a == b
    assert a[0].reasons == ("stop",)


def test_next_open_on_the_last_bar_takes_no_trade(research: ResearchSettings) -> None:
    s = _plain(research)
    day = _day([18000] * 4, [18001] * 4, [17999] * 4, [18000] * 4)
    assert run([day], NextOpen(at=3), VOL, s, "booker")[0] == []


def test_next_open_at_or_after_the_flat_time_is_refused(research: ResearchSettings) -> None:
    s = _plain(research)
    day = make_day(ny(2024, 3, 5, 15, 50), [18000.0] * 9)
    assert run([day], NextOpen(at=4), VOL, s, "booker")[0] == []  # next bar opens 15:55
    assert len(run([day], NextOpen(at=3), VOL, s, "booker")[0]) == 1  # 15:54 is fine


def test_close_entry_is_unchanged_by_default(research: ResearchSettings) -> None:
    sig = Signal(1, ny(2024, 3, 5, 15, 55))
    assert not sig.next_open
    assert sig.range_points is None
