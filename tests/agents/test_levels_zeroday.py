"""Grant (levels), Nadia's chase rule and Nia (zero-day), tested from the spec:
levels scoring G10 + spec "Levels + range definitions"; zero-day G9."""

from datetime import UTC, date, datetime, timedelta
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


@pytest.fixture
def grant(strategy: StrategyProfile, instrument: InstrumentProfile) -> LevelsReader:
    return LevelsReader(strategy.levels, strategy.trend, instrument)


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


def prior_and_today() -> list[Bar]:
    """Mon cash 06:30-06:34, Mon night 15:00-15:02, Tue 03:57-03:59, Tue cash 06:30-06:32."""
    bars = [bar(pt(MON, 6, 30) + MIN * i, 1000, 1010 + i, 990 - i, 1000 + i) for i in range(5)]
    bars += [bar(pt(MON, 15, 0) + MIN * i, 1004, 1030, 1001, 1004) for i in range(3)]
    bars += [bar(pt(TUE, 3, 57) + MIN * i, 1004, 1005, 960, 1004) for i in range(3)]
    bars += [
        bar(pt(TUE, 6, 30), 1004, 1006, 1002, 1004, volume=100),  # typical 1004
        bar(pt(TUE, 6, 31), 1006, 1012, 1006, 1012, volume=300),  # typical 1010
        bar(pt(TUE, 6, 32), 1010, 1013, 1009, 1011, volume=100),  # typical 1011
    ]
    return bars


def test_snapshot_computes_each_level(grant: LevelsReader) -> None:
    s = grant.snapshot(prior_and_today(), [], pt(TUE, 6, 33))
    assert isinstance(s, LevelsSnapshot)
    walls = {lv.name: lv.price for lv in s.walls}
    assert walls["prior_high"] == D(1014)
    assert walls["prior_low"] == D(986)
    assert walls["prior_close"] == D(1004)
    assert walls["overnight_high"] == D(1030)
    assert walls["overnight_low"] == D(960)
    # VWAP = (1004*100 + 1010*300 + 1011*100) / 500 = 1009
    assert s.vwap == D(1009) == walls["vwap"]
    assert s.price == D(1011)
    assert (s.round_below, s.round_above) == (D(1000), D(1100))


def test_round_numbers_are_reported_but_never_walls(grant: LevelsReader) -> None:
    s = grant.snapshot(prior_and_today(), [], pt(TUE, 6, 33))
    assert isinstance(s, LevelsSnapshot)
    assert all(lv.price != s.round_above for lv in s.walls)


def test_snapshot_ignores_the_forming_bar(grant: LevelsReader) -> None:
    bars = [*prior_and_today(), bar(pt(TUE, 6, 33), 1011, 5000, 1, 4000, volume=10**6)]
    s = grant.snapshot(bars, [], pt(TUE, 6, 33) + timedelta(seconds=59))
    assert isinstance(s, LevelsSnapshot)
    assert s.price == D(1011)
    assert s.vwap == D(1009)


def test_snapshot_fails_closed(grant: LevelsReader) -> None:
    bars = prior_and_today()
    before_open = grant.snapshot(bars, [], pt(TUE, 6, 30))
    assert isinstance(before_open, Unavailable)
    assert "VWAP" in before_open.reason
    no_prior = grant.snapshot([b for b in bars if b.ts >= pt(MON, 15, 0)], [], pt(TUE, 6, 33))
    assert isinstance(no_prior, Unavailable)
    assert "prior" in no_prior.reason
    no_night = [b for b in bars if not pt(MON, 15, 0) <= b.ts <= pt(TUE, 3, 59)]
    missing = grant.snapshot(no_night, [], pt(TUE, 6, 33))
    assert isinstance(missing, Unavailable)
    assert "overnight" in missing.reason
    assert isinstance(grant.snapshot([], [], pt(TUE, 6, 33)), Unavailable)


def test_snapshot_adds_trend_swings(grant: LevelsReader, strategy: StrategyProfile) -> None:
    tf = timedelta(minutes=strategy.trend.timeframe_minutes)
    start = pt(MON, 7, 0)
    highs = [1000, 1001, 1050, 1001, 1000, 999, 998]  # one swing high at 1050
    lows = [990, 989, 988, 989, 940, 989, 990]  # one swing low at 940
    trend = [
        bar(start + tf * i, lo + 1, hi, lo, lo + 1)
        for i, (hi, lo) in enumerate(zip(highs, lows, strict=True))
    ]
    s = grant.snapshot(prior_and_today(), trend, pt(TUE, 6, 33))
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


def calm_sessions(sessions: int = 21) -> list[Bar]:
    """06:25-06:59 PT each weekday: a steady zig-zag, volume 100."""
    bars: list[Bar] = []
    d = FIRST
    while len({b.ts.astimezone(PT).date() for b in bars}) < sessions:
        if d.weekday() < 5:
            for i in range(35):
                mid = 20000 + (1 if i % 2 else -1)
                bars.append(bar(pt(d, 6, 25) + MIN * i, mid, mid + 2, mid - 2, mid))
        d += timedelta(days=1)
    return bars


def last_day(bars: list[Bar]) -> date:
    return bars[-1].ts.astimezone(PT).date()


@pytest.fixture
def nia(strategy: StrategyProfile, instrument: InstrumentProfile) -> ZeroDayAgent:
    return ZeroDayAgent(strategy.zeroday, instrument)


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


def test_after_the_cash_close_prior_session_is_still_yesterday(grant: LevelsReader) -> None:
    # Prime runs past the cash close: today's finished cash session is not "prior".
    bars = [*prior_and_today(), bar(pt(TUE, 12, 59), 1011, 1500, 1011, 1400)]
    s = grant.snapshot(bars, [], pt(TUE, 13, 30))
    assert isinstance(s, LevelsSnapshot)
    walls = {lv.name: lv.price for lv in s.walls}
    assert walls["prior_high"] == D(1014)


def test_volatility_counts_the_gap_from_the_previous_close(nia: ZeroDayAgent) -> None:
    bars = calm_sessions()
    ts = pt(last_day(bars), 6, 50)
    # A normal 4-point bar, but it opened 16 points above the previous close.
    gapped = replace_bar(bars, ts, open_=20015, high=20017, low=20013, close=20015)
    check = nia.check(gapped, ts + MIN)
    assert any(f.startswith("volatility") for f in check.findings if "typical (limit" in f)
