"""Grant (levels), Nadia's chase rule and Nia (zero-day), tested from the spec:
levels scoring G10 + spec "Levels + range definitions"; zero-day G9."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.levels import (
    Level,
    LevelsReader,
    LevelsSnapshot,
    Unavailable,
    points_for_target,
)
from footprint.agents.range_reader import RangeReader
from footprint.agents.zero_day import ZeroDayAgent
from footprint.core.config import InstrumentProfile, RiskProfile, StrategyProfile
from footprint.core.models import Bar, Direction, Score
from tests.agents.conftest import bar

PT = ZoneInfo("America/Los_Angeles")
MIN = timedelta(minutes=1)
D = Decimal


def pt(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=PT).astimezone(UTC)


MON, TUE = date(2026, 9, 14), date(2026, 9, 15)


# --- N --------------------------------------------------------------------------


def test_points_for_target_uses_the_assumed_delta(
    risk: RiskProfile, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    n = points_for_target(
        risk.profit_target, strategy.options.assumed_delta, instrument.point_value, 1
    )
    # 1000 / (0.45 * 20) = 111.11...
    assert n.quantize(D("0.01")) == D("111.11")
    two = points_for_target(
        risk.profit_target, strategy.options.assumed_delta, instrument.point_value, 2
    )
    assert two.quantize(D("0.01")) == D("55.56")
    with pytest.raises(ValueError, match="contracts"):
        points_for_target(D(1), D("0.5"), D(1), 0)


# --- Grant: scoring -------------------------------------------------------------


def snap(price: float, *walls: tuple[str, float]) -> LevelsSnapshot:
    return LevelsSnapshot(
        as_of=pt(TUE, 7, 0),
        price=D(str(price)),
        vwap=D(str(price)) - 10,
        walls=tuple(Level(name, D(str(p))) for name, p in walls),
        round_below=D(0),
        round_above=D(0),
    )


N = D(100)


@pytest.mark.parametrize(
    ("wall", "expected"),
    [
        (1150, Score.BULLISH),  # exactly 1.5N away: full room
        (1149.75, Score.NEUTRAL),  # just under 1.5N
        (1100, Score.NEUTRAL),  # exactly N: not a wall
        (1099.75, Score.BEARISH),  # just under N: wall before the target -> veto
    ],
)
def test_levels_long_boundaries(grant: LevelsReader, wall: float, expected: Score) -> None:
    read = grant.assess(snap(1000, ("prior_high", wall), ("far", 5000)), Direction.LONG, N)
    assert read.score is expected


@pytest.mark.parametrize(
    ("wall", "expected"),
    [(850, Score.BULLISH), (900, Score.NEUTRAL), (900.25, Score.BEARISH)],
)
def test_levels_short_mirror(grant: LevelsReader, wall: float, expected: Score) -> None:
    read = grant.assess(snap(1000, ("prior_low", wall)), Direction.SHORT, N)
    assert read.score is expected


def test_levels_use_the_nearest_opposing_level_only(grant: LevelsReader) -> None:
    # Levels behind the trade never block it; the nearest one ahead decides.
    s = snap(1000, ("behind", 990), ("near", 1050), ("far", 1500))
    assert grant.assess(s, Direction.LONG, N).score is Score.BEARISH
    assert "near" in grant.assess(s, Direction.LONG, N).reason
    # For a short, 990 is the level ahead: 10 points away, a wall.
    assert grant.assess(s, Direction.SHORT, N).score is Score.BEARISH


def test_levels_open_room_when_nothing_is_ahead(grant: LevelsReader) -> None:
    assert grant.assess(snap(1000, ("below", 900)), Direction.LONG, N).score is Score.BULLISH


def test_unavailable_levels_veto(grant: LevelsReader) -> None:
    read = grant.assess(Unavailable(pt(TUE, 5, 0), "no VWAP"), Direction.LONG, N)
    assert read.score is Score.BEARISH
    assert "no VWAP" in read.reason


def test_levels_reject_bad_n(grant: LevelsReader) -> None:
    with pytest.raises(ValueError, match="n_points"):
        grant.assess(snap(1000), Direction.LONG, D(0))


# --- Grant: snapshot from bars --------------------------------------------------

Quote = tuple[float, float, float, float, int]


def flat_minutes(
    start: datetime, end: datetime, price: float, overrides: dict[datetime, Quote] | None = None
) -> list[Bar]:
    """One bar per minute in [start, end): open=close=price, range +/-1, volume 100."""
    overrides = overrides or {}
    out, ts = [], start
    while ts < end:
        o, h, lo, c, v = overrides.get(ts, (price, price + 1, price - 1, price, 100))
        out.append(bar(ts, o, h, lo, c, v))
        ts += MIN
    return out


def session_data(prev: date, today: date, *, prior_close: float = 1004) -> list[Bar]:
    """Everything Grant needs at 06:33 on `today`: the previous trading day from 04:00,
    the full overnight session, today's pre-market and three cash bars.

    Prior cash session: high 1014, low 986, close `prior_close`.
    Overnight session: high 1030, low 960. Today's cash VWAP: 1009, last close 1011.
    """
    bars = flat_minutes(pt(prev, 4, 0), pt(prev, 6, 30), 1000)
    bars += flat_minutes(
        pt(prev, 6, 30),
        pt(prev, 13, 0),
        1000,
        {
            pt(prev, 7, 0): (1000, 1014, 999, 1000, 100),
            pt(prev, 8, 0): (1000, 1001, 986, 1000, 100),
            pt(prev, 12, 59): (1000, prior_close + 1, 999, prior_close, 100),
        },
    )
    bars += flat_minutes(pt(prev, 13, 0), pt(prev, 14, 0), 1004)
    night_start = datetime.combine(today - timedelta(days=1), datetime.min.time(), PT)
    bars += flat_minutes(
        (night_start + timedelta(hours=15)).astimezone(UTC),
        pt(today, 4, 0),
        1004,
        {
            (night_start + timedelta(hours=20)).astimezone(UTC): (1004, 1030, 1003, 1004, 100),
            pt(today, 2, 0): (1004, 1005, 960, 1004, 100),
        },
    )
    bars += flat_minutes(pt(today, 4, 0), pt(today, 6, 30), 1004)
    bars += [
        bar(pt(today, 6, 30), 1004, 1006, 1002, 1004, volume=100),  # typical 1004
        bar(pt(today, 6, 31), 1006, 1012, 1006, 1012, volume=300),  # typical 1010
        bar(pt(today, 6, 32), 1010, 1013, 1009, 1011, volume=100),  # typical 1011
    ]
    return bars


@pytest.fixture
def grant(strategy: StrategyProfile, instrument: InstrumentProfile) -> LevelsReader:
    return LevelsReader(strategy.levels, strategy.trend, strategy.data, instrument)


def grant_at(
    grant: LevelsReader, bars: list[Bar], as_of: datetime, strategy: StrategyProfile
) -> LevelsSnapshot | Unavailable:
    return grant.snapshot(bars, as_of)


def walls_of(s: LevelsSnapshot | Unavailable) -> dict[str, Decimal]:
    assert isinstance(s, LevelsSnapshot), s
    return {lv.name: lv.price for lv in s.walls if not lv.name.startswith("swing")}


def test_snapshot_computes_each_level(grant: LevelsReader, strategy: StrategyProfile) -> None:
    s = grant_at(grant, session_data(MON, TUE), pt(TUE, 6, 33), strategy)
    assert walls_of(s) == {
        "prior_high": D(1014),
        "prior_low": D(986),
        "prior_close": D(1004),
        "overnight_high": D(1030),
        "overnight_low": D(960),
        # VWAP = (1004*100 + 1010*300 + 1011*100) / 500 = 1009
        "vwap": D(1009),
    }
    assert isinstance(s, LevelsSnapshot)
    assert s.price == D(1011)
    assert (s.round_below, s.round_above) == (D(1000), D(1100))


def test_round_numbers_are_reported_but_never_walls(
    grant: LevelsReader, strategy: StrategyProfile
) -> None:
    s = grant_at(grant, session_data(MON, TUE), pt(TUE, 6, 33), strategy)
    assert isinstance(s, LevelsSnapshot)
    assert all(lv.price not in {s.round_above, s.round_below} for lv in s.walls)


def test_snapshot_ignores_the_forming_bar(grant: LevelsReader, strategy: StrategyProfile) -> None:
    bars = [*session_data(MON, TUE), bar(pt(TUE, 6, 33), 1011, 5000, 1, 4000, volume=10**6)]
    s = grant_at(grant, bars, pt(TUE, 6, 33) + timedelta(seconds=59), strategy)
    assert isinstance(s, LevelsSnapshot)
    assert s.price == D(1011)
    assert s.vwap == D(1009)


def test_monday_uses_friday_and_the_sunday_night_session(
    grant: LevelsReader, strategy: StrategyProfile
) -> None:
    fri, mon = date(2026, 9, 11), date(2026, 9, 14)
    s = grant_at(grant, session_data(fri, mon), pt(mon, 6, 33), strategy)
    assert walls_of(s)["prior_high"] == D(1014)
    assert walls_of(s)["overnight_low"] == D(960)


def test_day_after_a_holiday_uses_the_last_trading_day(
    grant: LevelsReader, strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    # Monday is a holiday: Tuesday's prior day is Friday. Monday's evening session leads
    # into Tuesday, so the overnight levels come from Monday night.
    fri, mon, tue = date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)
    closed = replace(instrument, holidays=frozenset({mon}))
    g = LevelsReader(strategy.levels, strategy.trend, strategy.data, closed)
    friday = [b for b in session_data(fri, mon) if b.ts < pt(fri, 14, 0)]
    bars = friday + [b for b in session_data(mon, tue) if b.ts >= pt(mon, 15, 0)]
    s = g.snapshot(bars, pt(tue, 6, 33))
    assert walls_of(s)["prior_high"] == D(1014)
    assert walls_of(s)["overnight_high"] == D(1030)
    # Without the holiday entry, Monday would be required and is missing: fail closed.
    assert isinstance(grant.snapshot(bars, pt(tue, 6, 33)), Unavailable)


def test_monday_after_daylight_saving_ends(grant: LevelsReader, strategy: StrategyProfile) -> None:
    fri, mon = date(2026, 10, 30), date(2026, 11, 2)  # clocks fall back Sun Nov 1
    s = grant_at(grant, session_data(fri, mon), pt(mon, 6, 33), strategy)
    assert walls_of(s)["prior_low"] == D(986)
    assert walls_of(s)["overnight_high"] == D(1030)


def test_after_the_cash_close_prior_session_is_still_yesterday(
    grant: LevelsReader, strategy: StrategyProfile
) -> None:
    bars = session_data(MON, TUE)
    bars += flat_minutes(
        pt(TUE, 6, 33), pt(TUE, 13, 30), 1011, {pt(TUE, 9, 0): (1011, 1500, 1010, 1011, 100)}
    )
    s = grant_at(grant, bars, pt(TUE, 13, 30), strategy)
    assert walls_of(s)["prior_high"] == D(1014)


def test_wall_exactly_at_the_price_vetoes(grant: LevelsReader) -> None:
    read = grant.assess(snap(1014, ("prior_high", 1014)), Direction.LONG, N)
    assert read.score is Score.BEARISH
    read = grant.assess(snap(986, ("prior_low", 986)), Direction.SHORT, N)
    assert read.score is Score.BEARISH


@pytest.mark.parametrize(
    ("when", "reason"),
    [
        (pt(TUE, 14, 30), "outside the prime session"),
        (pt(date(2026, 9, 19), 7, 0), "outside the prime session"),  # Saturday
        (pt(TUE, 6, 30), "VWAP"),  # cash not open yet
        (pt(TUE, 6, 36), "stale feed"),  # newest bar closed 3 minutes ago
    ],
)
def test_snapshot_fails_closed_on_time_and_freshness(
    grant: LevelsReader, strategy: StrategyProfile, when: datetime, reason: str
) -> None:
    s = grant_at(grant, session_data(MON, TUE), when, strategy)
    assert isinstance(s, Unavailable)
    assert reason in s.reason


def test_snapshot_fails_closed_on_missing_data(grant: LevelsReader) -> None:
    bars = session_data(MON, TUE)
    as_of = pt(TUE, 6, 33)

    def reason(subset: list[Bar], when: datetime = as_of) -> str:
        s = grant.snapshot(subset, when)
        assert isinstance(s, Unavailable)
        return s.reason

    def without(start: datetime, end: datetime, source: list[Bar] = bars) -> list[Bar]:
        return [b for b in source if not start <= b.ts < end]

    prior = reason(without(pt(MON, 9, 0), pt(MON, 10, 0)))
    assert "cash session: 330 of 390 minutes" in prior
    gap = reason(without(pt(MON, 9, 0), pt(MON, 9, 10)))  # 380 of 390, but a 10-min hole
    assert "cash session" in gap
    assert "consecutive missing" in gap
    assert "final minute is missing" in reason(without(pt(MON, 12, 59), pt(MON, 13, 0)))
    assert "0 of 390" in reason(without(pt(MON, 6, 30), pt(MON, 13, 0)))
    assert "overnight" in reason(without(pt(TUE, 0, 0), pt(TUE, 2, 0)))
    night_tail = reason(without(pt(TUE, 3, 50), pt(TUE, 4, 0)))
    assert "overnight" in night_tail
    assert "consecutive missing" in night_tail
    trend = reason(without(pt(MON, 4, 0), pt(MON, 6, 30)))
    assert "of 96 completed trend bars" in trend
    assert "gap in trend bars" in reason(without(pt(TUE, 4, 30), pt(TUE, 5, 0)))
    assert "no completed" in reason([])


def test_vwap_needs_today_s_cash_minutes(grant: LevelsReader) -> None:
    bars = session_data(MON, TUE)
    bars += flat_minutes(pt(TUE, 6, 33), pt(TUE, 6, 45), 1011)
    holed = [b for b in bars if not pt(TUE, 6, 31) <= b.ts < pt(TUE, 6, 39)]
    s = grant.snapshot(holed, pt(TUE, 6, 45))
    assert isinstance(s, Unavailable)
    assert "VWAP" in s.reason
    assert isinstance(grant.snapshot(bars, pt(TUE, 6, 45)), LevelsSnapshot)


def test_snapshot_adds_trend_swings(grant: LevelsReader) -> None:
    planted = {
        pt(MON, 10, 7): (1000.0, 1050.0, 999.0, 1000.0),
        pt(MON, 11, 37): (1000.0, 1001.0, 940.0, 1000.0),
    }
    bars = [bar(b.ts, *planted[b.ts]) if b.ts in planted else b for b in session_data(MON, TUE)]
    s = grant.snapshot(bars, pt(TUE, 6, 33))
    assert isinstance(s, LevelsSnapshot)
    names = {(lv.name, lv.price) for lv in s.walls}
    assert ("swing_high", D(1050)) in names
    assert ("swing_low", D(940)) in names


# --- Nadia: chase rule ----------------------------------------------------------


def days(today_low: float, today_high: float, close: float) -> list[Bar]:
    history = [
        bar(pt(MON, 6, 30) - timedelta(days=11 - i), 1000, 1100, 1000, 1050) for i in range(10)
    ]
    return [*history, bar(pt(MON, 6, 30), today_low, today_high, today_low, close)]


def test_range_chase_rule(strategy: StrategyProfile) -> None:
    nadia = RangeReader(strategy.range)
    # 80% of ADR used, closing in the top 10% of the range.
    stretched_at_top = days(1000, 1080, 1075)
    assert nadia.assess(stretched_at_top, Direction.LONG).score is Score.BEARISH
    assert "chasing" in nadia.assess(stretched_at_top, Direction.LONG).reason
    assert nadia.assess(stretched_at_top, Direction.SHORT).score is Score.NEUTRAL
    # Same range, closing mid-range: no chase.
    assert nadia.assess(days(1000, 1080, 1040), Direction.LONG).score is Score.NEUTRAL
    # Quiet day (50% used) at the top: still room to run.
    assert nadia.assess(days(1000, 1050, 1050), Direction.LONG).score is Score.BULLISH
    # Short side mirror: 80% used, closing in the bottom 10%.
    assert nadia.assess(days(1000, 1080, 1005), Direction.SHORT).score is Score.BEARISH
    # Extended day stays -1 either way.
    assert nadia.assess(days(1000, 1101, 1050), Direction.SHORT).score is Score.BEARISH


def test_range_chase_needs_history(strategy: StrategyProfile) -> None:
    short = days(1000, 1080, 1079)[-5:]
    assert RangeReader(strategy.range).assess(short, Direction.LONG).score is Score.NEUTRAL


# --- Nia ------------------------------------------------------------------------


FIRST = date(2026, 8, 3)  # a Monday


def calm_sessions(
    sessions: int = 21, start: tuple[int, int] = (6, 25), count: int = 35
) -> list[Bar]:
    """`count` minutes from `start` PT each weekday: a steady zig-zag, volume 100."""
    bars: list[Bar] = []
    d, seen = FIRST, 0
    while seen < sessions:
        if d.weekday() < 5:
            seen += 1
            for i in range(count):
                mid = 20000 + (1 if i % 2 else -1)
                bars.append(bar(pt(d, *start) + MIN * i, mid, mid + 2, mid - 2, mid))
        d += timedelta(days=1)
    return bars


def last_day(bars: list[Bar]) -> date:
    return bars[-1].ts.astimezone(PT).date()


@pytest.fixture
def nia(strategy: StrategyProfile, instrument: InstrumentProfile) -> ZeroDayAgent:
    return ZeroDayAgent(strategy.zeroday, strategy.data, instrument)


def at(bars: list[Bar], minute: int) -> datetime:
    """as_of just after the bar at 06:<minute> on the last day closes."""
    return pt(last_day(bars), 6, minute) + MIN


def test_calm_market_does_not_trip(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    check = nia.check(bars, at(bars, 50))
    assert not check.tripped, check.findings
    assert any("spread: not measured" in f for f in check.findings)


def replace_bar(bars: list[Bar], ts: datetime, **ohlcv: float) -> list[Bar]:
    out = []
    for b in bars:
        if b.ts == ts:
            values = {"open_": float(b.open), "high": float(b.high), "low": float(b.low)}
            values |= {"close": float(b.close)}
            values.update({k: v for k, v in ohlcv.items() if k != "volume"})
            out.append(bar(ts, volume=int(ohlcv.get("volume", b.volume)), **values))  # type: ignore[arg-type]
        else:
            out.append(b)
    return out


def test_volume_tripwire_uses_its_own_multiple(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    nine = nia.check(replace_bar(bars, ts, volume=900), ts + MIN)
    assert not nine.tripped  # a 9x spike is a momentum trigger, not an unknown market
    eleven = nia.check(replace_bar(bars, ts, volume=1100), ts + MIN)
    assert eleven.tripped
    assert any(f.startswith("volume") for f in eleven.findings)


def test_volatility_tripwire(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    # Typical true range is 4 points; a 20-point bar that closes where it opened.
    prev_close = float(next(b for b in bars if b.ts == ts - MIN).close)
    assert prev_close == 19999
    wild = replace_bar(bars, ts, open_=19999, high=20009, low=19989, close=20001)
    check = nia.check(wild, ts + MIN)
    assert check.tripped
    assert any(f.startswith("volatility") for f in check.findings)


def test_jump_tripwire(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    # Typical close-to-close move is 2 points; jump 12 points.
    jumped = replace_bar(bars, ts, open_=20011, high=20013, low=20009, close=20011)
    check = nia.check(jumped, ts + MIN)
    assert check.tripped
    assert any(f.startswith("jump") for f in check.findings)


def test_range_tripwire(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 40)
    # Typical cash range is 4 points; widen one earlier cash bar today to 30 points.
    wide = replace_bar(bars, ts, high=20030)
    check = nia.check(wide, at(bars, 50))
    assert check.tripped
    assert any(f.startswith("range") for f in check.findings)


def test_unknown_history_trips(nia: ZeroDayAgent) -> None:
    bars = calm_sessions(5)
    check = nia.check(bars, at(bars, 50))
    assert check.tripped
    assert any("cannot establish" in f for f in check.findings)
    assert nia.check(bars[:1], at(bars, 50)).tripped


def test_nia_ignores_the_forming_bar(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    forming = replace_bar(bars, ts, volume=10**6)
    assert not nia.check(forming, ts + timedelta(seconds=59)).tripped


def test_volatility_counts_the_gap_from_the_previous_close(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    # A normal 4-point bar, but it opened 16 points above the previous close.
    gapped = replace_bar(bars, ts, open_=20015, high=20017, low=20013, close=20015)
    check = nia.check(gapped, ts + MIN)
    assert any(f.startswith("volatility") for f in check.findings if "typical (limit" in f)


def test_nia_judges_range_before_the_cash_open(nia: ZeroDayAgent) -> None:
    # A steady pre-market slide made of normal-sized bars: only the range wire sees it.
    bars = calm_sessions(start=(4, 0), count=180)
    today = last_day(bars)
    slide = {
        pt(today, 4, 0) + MIN * i: (20000 - 2 * i, 20002 - 2 * i, 19998 - 2 * i, 20000 - 2 * i)
        for i in range(150)
    }
    bars = [
        bar(b.ts, *slide[b.ts]) if b.ts in slide else b for b in bars if b.ts < pt(today, 6, 30)
    ]
    check = nia.check(bars, pt(today, 6, 30))
    assert check.tripped
    assert next(f for f in check.findings if "typical (limit" in f).startswith("range")


def test_nia_keeps_judging_range_after_the_cash_close(nia: ZeroDayAgent) -> None:
    bars = calm_sessions(start=(12, 50), count=30)  # 12:50-13:19 each day
    today = last_day(bars)
    wide = replace_bar(bars, pt(today, 12, 55), high=20100)
    check = nia.check(wide, pt(today, 13, 20))
    assert check.tripped
    assert any(f.startswith("range") for f in check.findings)


def test_nia_trips_outside_prime_and_on_a_stale_feed(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    today = last_day(bars)
    assert nia.check(bars, pt(today, 14, 30)).tripped
    stale = nia.check(bars, pt(today, 7, 10))  # newest bar closed at 07:00
    assert stale.tripped
    assert "stale feed" in stale.findings[0]


def test_nia_trips_outside_prime_even_on_a_fresh_calm_feed(nia: ZeroDayAgent) -> None:
    bars = calm_sessions(start=(14, 0), count=30)  # 14:00-14:29: after the prime close
    check = nia.check(bars, pt(last_day(bars), 14, 30))
    assert check.tripped
    assert "outside the prime session" in check.findings[0]


def test_nia_range_history_uses_the_same_clock_window(nia: ZeroDayAgent) -> None:
    # Earlier days had a huge spike after today's cut-off time; it must not inflate
    # "typical" for a window that ends before it.
    bars = calm_sessions()
    today = last_day(bars)
    spiked = [
        bar(b.ts, float(b.open), float(b.high) + 100, float(b.low), float(b.close))
        if b.ts.astimezone(PT).time() == time(6, 55) and b.ts.astimezone(PT).date() != today
        else b
        for b in bars
    ]
    wide = replace_bar(spiked, pt(today, 6, 30), high=20030)
    check = nia.check(wide, pt(today, 6, 40))
    assert check.tripped
    assert any(f.startswith("range") and "typical (limit" in f for f in check.findings)


def test_nia_range_needs_full_trading_day_history(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    # One of the 20 history days is a holiday: the same-time wires still have 20 bars,
    # but range has only 19 trading days and must refuse to judge.
    bars = calm_sessions()
    closed = replace(instrument, holidays=frozenset({FIRST}))
    check = ZeroDayAgent(strategy.zeroday, strategy.data, closed).check(bars, at(bars, 50))
    assert check.tripped
    assert "range: cannot establish a typical value" in check.findings


def test_a_trend_bar_built_from_too_few_minutes_is_rejected(grant: LevelsReader) -> None:
    # 13:00-14:00 is outside the cash session, so only the trend check sees this hole.
    bars = [b for b in session_data(MON, TUE) if not pt(MON, 13, 16) <= b.ts < pt(MON, 13, 30)]
    s = grant.snapshot(bars, pt(TUE, 6, 33))
    assert isinstance(s, Unavailable)
    assert "thin trend bar" in s.reason


def test_a_bucket_straddling_the_daily_halt_counts_only_open_minutes(
    grant: LevelsReader,
) -> None:
    # A print in the 14:00 minute makes a 14:00 bucket with one open minute; the other
    # fourteen are the exchange's daily halt and must not count as missing.
    bars = sorted(
        [*session_data(MON, TUE), bar(pt(MON, 14, 0), 1004, 1005, 1003, 1004)], key=lambda b: b.ts
    )
    assert isinstance(grant.snapshot(bars, pt(TUE, 6, 33)), LevelsSnapshot)


def test_an_extreme_bar_keeps_nia_tripped_for_the_recent_window(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    today = last_day(bars)
    spiked = replace_bar(bars, pt(today, 6, 45), volume=1500)
    assert nia.check(spiked, pt(today, 6, 50)).tripped  # 5 closed bars include 06:45
    assert not nia.check(spiked, pt(today, 6, 51)).tripped  # 06:46-06:50 only
