"""BH-11: a BarWindow (a run of a checked BarSeries, the backtest's decision window)
gives every series helper exactly the answer the same bars give as a plain list.

The window helpers reuse work done once over the whole series. Each answer here is
compared with the same bars handed over as a list (never cached, never windowed), on
windows that cut buckets and days at both ends, at moments before the window's last
bar has closed, and with different bars after the window in the series: a helper that
read one bar outside its window (look-ahead included) would answer differently.
"""

from __future__ import annotations

import copy
import pickle
import random
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from footprint.agents._series import (
    last_two_swing_points,
    relative_volume,
    same_time_indices,
    swing_points,
)
from footprint.agents.levels import LevelsReader
from footprint.agents.regime_reader import RegimeReader
from footprint.agents.zero_day import ZeroDayAgent
from footprint.core.config import Profiles, Session, load_profiles
from footprint.core.models import Bar
from footprint.core.series import BarSeries, BarWindow, require_series
from footprint.core.timecache import local_time
from footprint.data.aggregate import completed, resample, session_days
from footprint.decision.pipeline import Pipeline
from tests.backtest.speed_market import speed_market
from tests.conftest import REPO_ROOT
from tests.test_series_caches import TZ, random_bars

D = Decimal
PT = ZoneInfo(TZ)

MINUTE = timedelta(minutes=1)


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


def _windows(series: BarSeries, rng: random.Random, count: int) -> list[BarWindow]:
    n = len(series.bars)
    out = [series.window(0, n), series.window(0, 1), series.window(n - 1, n)]
    for _ in range(count):
        start = rng.randrange(n)
        out.append(series.window(start, rng.randrange(start + 1, n + 1)))
    return out


def _moments(window: BarWindow, rng: random.Random) -> list[datetime]:
    """After the last bar closed, exactly when it closes, while it is open, and at a
    random bar inside the window (most of the window not closed yet)."""
    last = window[-1].ts
    inside = window[rng.randrange(len(window))].ts
    return [last + MINUTE * 3, last + MINUTE, last, inside, inside + MINUTE]


def test_a_window_is_the_plain_tuple_of_its_bars() -> None:
    bars = random_bars(1, 200)
    series = BarSeries(bars, 1, "bars")
    window = series.window(20, 90)
    assert window == tuple(bars[20:90])
    assert (window.start, window.end, len(window)) == (20, 90, 70)
    assert window[3:7] == tuple(bars[23:27])
    assert type(window[3:7]) is tuple  # a slice of a window is a plain tuple
    assert series.window(5, 5) == ()
    for start, end in ((-1, 3), (4, 3), (0, 201)):
        with pytest.raises(ValueError, match="outside a series"):
            series.window(start, end)


def test_a_series_is_checked_once_and_refuses_what_require_series_refuses() -> None:
    good = random_bars(2, 50)
    backwards = [*good[:10], good[5], *good[11:]]
    with pytest.raises(ValueError, match="strictly increasing"):
        BarSeries(backwards, 1, "bars")
    with pytest.raises(ValueError, match="must not be empty"):
        BarSeries([], 1, "bars")
    series = BarSeries(good, 1, "bars")
    assert series.starts == tuple(b.ts for b in good)


def test_require_series_on_a_window_passes_what_it_passes_and_refuses_the_rest() -> None:
    series = BarSeries(random_bars(3, 300), 1, "bars")
    window = series.window(10, 200)
    require_series(window, 1, "bars")
    require_series(window, None, "bars")
    with pytest.raises(ValueError, match="must not be empty"):
        require_series(series.window(7, 7), 1, "bars")
    # Another timeframe is checked as for any tuple (the series was checked for 1 min).
    with pytest.raises(ValueError, match="not a multiple of 15"):
        require_series(window, 15, "bars")
    with pytest.raises(ValueError, match="not a multiple of 15"):
        require_series(list(window), 15, "bars")


@pytest.mark.parametrize("seed", range(4))
def test_completed_on_a_window_is_the_same_bars(seed: int) -> None:
    rng = random.Random(seed)
    series = BarSeries(random_bars(seed, 3000), 1, "bars")
    for window in _windows(series, rng, 40):
        for as_of in _moments(window, rng):
            for minutes in (1, 15):
                got = completed(window, as_of, minutes)
                assert got == completed(list(window), as_of, minutes)
                if all(b.ts + timedelta(minutes=minutes) <= as_of for b in window):
                    assert got is window  # the same object: the readers' caches share it


@pytest.mark.parametrize("seed", range(6))
def test_resample_of_a_window_equals_the_uncached_resample(seed: int) -> None:
    rng = random.Random(seed)
    series = BarSeries(random_bars(seed, 4000), 1, "bars")
    for window in _windows(series, rng, 40):
        for as_of in _moments(window, rng):
            for minutes in (1, 5, 15, 60):
                assert resample(window, minutes, as_of) == resample(list(window), minutes, as_of)


def test_resample_of_a_window_never_reads_a_bar_past_its_end() -> None:
    """A window ending inside a bucket: the series' bars after it (here different from
    a series that stops there) never reach the window's last bucket."""
    bars = random_bars(5, 2000)
    long, short = BarSeries(bars, 1, "bars"), BarSeries(bars[:1007], 1, "bars")
    as_of = bars[-1].ts + timedelta(days=1)
    for start in (0, 3, 500, 1000):
        a, b = long.window(start, 1007), short.window(start, 1007)
        for minutes in (5, 15, 60, 240):
            assert resample(a, minutes, as_of) == resample(b, minutes, as_of)
            assert resample(a, minutes, as_of) == resample(list(a), minutes, as_of)


@pytest.mark.parametrize("seed", range(4))
def test_session_days_of_a_window_equal_the_uncached_ones(seed: int) -> None:
    rng = random.Random(seed)
    series = BarSeries(random_bars(seed, 5000), 1, "bars")
    sessions = (
        Session(start=time(6, 30), end=time(13, 0), timezone=TZ),
        Session(start=time(0, 0), end=time(23, 59), timezone="Europe/London"),
        Session(start=time(1, 0), end=time(3, 0), timezone=TZ),  # the DST hours
    )
    for window in _windows(series, rng, 30):
        for as_of in _moments(window, rng):
            for session in sessions:
                assert session_days(window, session, as_of) == session_days(
                    list(window), session, as_of
                )


def test_session_days_of_a_window_refuse_a_session_across_midnight_as_before() -> None:
    series = BarSeries(random_bars(6, 300), 1, "bars")
    night = Session(start=time(15, 0), end=time(3, 59), timezone=TZ)
    as_of = series.bars[-1].ts + MINUTE
    with pytest.raises(ValueError, match="does not cross midnight"):
        session_days(list(series.window(0, 300)), night, as_of)
    with pytest.raises(ValueError, match="does not cross midnight"):
        session_days(series.window(0, 300), night, as_of)
    # No completed bar: nothing to group, so nothing refused (as for a list).
    early = series.bars[0].ts
    assert session_days(series.window(0, 300), night, early) == session_days(
        list(series.window(0, 300)), night, early
    )


@pytest.mark.parametrize("seed", range(4))
def test_same_time_indices_and_relative_volume_of_a_window(seed: int) -> None:
    rng = random.Random(seed)
    series = BarSeries(random_bars(seed, 6000), 1, "bars")
    for window in _windows(series, rng, 30):
        for _ in range(10):
            index = rng.randrange(len(window))
            for lookback in (1, 3, 20):
                for zone in (TZ, "Europe/London"):
                    expected = same_time_indices(list(window), index, lookback, zone)
                    assert same_time_indices(window, index, lookback, zone) == expected
                assert relative_volume(window, index, lookback, TZ) == relative_volume(
                    list(window), index, lookback, TZ
                )


def _sessions(profiles: Profiles, first: date, count: int, seed: int) -> list[Bar]:
    return speed_market(profiles.instrument, first, count, seed)


def _decision_moments(window: BarWindow, rng: random.Random) -> list[datetime]:
    """Moments a decision is made at (the minute after a bar) and a few others."""
    picks = [window[-1].ts + MINUTE, window[-1].ts + MINUTE * 7]
    for _ in range(6):
        picks.append(window[rng.randrange(len(window))].ts + MINUTE)
    picks.append(window[rng.randrange(len(window))].ts + timedelta(seconds=30))
    return picks


@pytest.mark.parametrize("seed", range(3))
def test_the_zero_day_check_on_a_window_equals_the_list_check(
    profiles: Profiles, seed: int
) -> None:
    rng = random.Random(seed)
    bars = _sessions(profiles, date(2024, 2, 26), 30, seed)  # spans the DST change
    series = BarSeries(bars, 1, "bars")
    s = profiles.strategy
    agents = [
        ZeroDayAgent(s.zeroday, s.data, profiles.instrument),
        # a short look-back, so the edge days of a window are among the days compared
        ZeroDayAgent(replace(s.zeroday, lookback_sessions=3), s.data, profiles.instrument),
    ]
    n = len(bars)
    for _ in range(25):
        end = rng.randrange(n // 3, n + 1)
        start = max(0, end - rng.choice((300, 2000, 8000, 30000)))
        window = series.window(start, end)
        for as_of in _decision_moments(window, rng):
            for agent in agents:
                assert agent.check(window, as_of) == agent.check(list(window), as_of)


def test_the_zero_day_range_on_a_window_across_the_autumn_clock_change(
    profiles: Profiles,
) -> None:
    """A day whose local clock goes back (prime from 00:00, every weekday traded, so
    the autumn change at 01:00 falls inside a counted day): its clock times are not in
    order, so it is measured from the window's own bars, as for a list."""
    bars = random_bars(8, 4000)
    start = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    shifted = [replace(b, ts=start + MINUTE * i) for i, b in enumerate(bars)]
    series = BarSeries(shifted, 1, "bars")
    prime = replace(profiles.instrument.prime_session, start=time(0, 0))
    instrument = replace(
        profiles.instrument,
        prime_session=prime,
        trading_days=frozenset(range(7)),
        holidays=frozenset(),
        no_trade_days=frozenset(),
    )
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, instrument)
    rng = random.Random(8)
    seen_out_of_order = False
    for _ in range(40):
        end = rng.randrange(1, len(shifted) + 1)
        window = series.window(rng.randrange(end), end)
        cutoff = time(rng.randrange(24), rng.randrange(60))
        got = agent._window_spans(window, cutoff)
        assert got == agent._spans(list(window), cutoff)
        seen_out_of_order |= date(2026, 11, 1) in got
    assert seen_out_of_order


@pytest.mark.parametrize("seed", range(2))
def test_the_levels_snapshot_on_a_window_equals_the_list_snapshot(
    profiles: Profiles, seed: int
) -> None:
    rng = random.Random(seed)
    bars = _sessions(profiles, date(2024, 4, 1), 6, seed)
    series = BarSeries(bars, 1, "bars")
    s = profiles.strategy
    reader = LevelsReader(s.levels, s.trend, s.data, profiles.instrument)
    n = len(bars)
    for _ in range(25):
        end = rng.randrange(n // 2, n + 1)
        window = series.window(max(0, end - rng.choice((500, 3000, 43200))), end)
        for as_of in _decision_moments(window, rng):
            assert reader.snapshot(window, as_of) == reader.snapshot(list(window), as_of)


@pytest.mark.parametrize("seed", range(8))
def test_the_last_two_swing_points_are_the_last_two_of_all_swing_points(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(80):
        values = [Decimal(rng.randint(0, 9)) for _ in range(rng.randrange(0, 60))]
        for strength in (1, 2, 3):
            for high in (True, False):
                every = swing_points(values, strength, high=high)
                assert last_two_swing_points(values, strength, high=high) == every[-2:]


@pytest.mark.parametrize("seed", range(2))
def test_the_pipelines_range_days_on_a_window_equal_the_list_ones(
    profiles: Profiles, seed: int
) -> None:
    """After the cash close the range days take today's later bars (only those after
    the day before today's midnight are searched, for a window)."""
    rng = random.Random(seed)
    bars = _sessions(profiles, date(2024, 3, 4), 4, seed)  # spans the DST change
    series = BarSeries(bars, 1, "bars")
    pipeline = Pipeline(profiles)
    n = len(bars)
    for _ in range(40):
        end = rng.randrange(1, n + 1)
        window = series.window(max(0, end - rng.choice((100, 1500, 6000))), end)
        for as_of in _decision_moments(window, rng):
            assert pipeline._range_days(window, as_of) == pipeline._range_days(list(window), as_of)


def test_the_zero_day_range_keeps_the_first_of_equal_highs_and_lows(profiles: Profiles) -> None:
    """max() and min() keep the first of equal values; so must the running highs and
    lows, or a range could be written 10.00 where the list gives 10.0."""
    day = datetime(2024, 4, 2, 11, 0, tzinfo=UTC)  # 04:00 PT, a Tuesday
    spellings = ("20010.0", "20010.00", "20010", "20010.000")
    bars = [
        Bar("NQ", day + MINUTE * i, D("20005"), D(spellings[i % 4]), D(low), D("20005"), 1)
        for i, low in enumerate(["20000", "20000.0", "20000.00"] * 4)
    ]
    series = BarSeries([*bars, replace(bars[-1], ts=day + MINUTE * 99)], 1, "bars")
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, profiles.instrument)
    window = series.window(0, len(series.bars))
    for minute in range(1, 14):
        cutoff = (day + MINUTE * minute).astimezone(PT).time()
        got = {d: str(v) for d, v in agent._window_spans(window, cutoff).items()}
        assert got == {d: str(v) for d, v in agent._spans(list(window), cutoff).items()}


def test_the_regime_reader_on_a_window_equals_the_list_reader(profiles: Profiles) -> None:
    """The play caller's history is a window from the series' start (its checked
    start times are reused; nothing past the window is read)."""
    bars = _sessions(profiles, date(2024, 3, 11), 63, 3)
    series = BarSeries(bars, 1, "bars")
    cash = profiles.instrument.cash_session
    labelled = 0
    for day in (date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)):
        due = datetime.combine(day, cash.start, PT).astimezone(UTC)
        due += MINUTE * profiles.regime.decision_minutes
        for as_of in (due, due + MINUTE * 3):
            end = bisect_right(series.starts, as_of - MINUTE)
            windows = (series.window(0, end), series.window(0, len(bars)), series.window(9, end))
            for window in windows:
                kwargs = (profiles.regime, profiles.strategy.data, profiles.instrument)
                got = RegimeReader(*kwargs).call(window, as_of)
                assert got == RegimeReader(*kwargs).call(list(window), as_of)
                labelled += got.efficiency_rank_pct is not None
    assert labelled  # real labels were compared, not only refusals


def _across_a_date_going_back() -> BarSeries:
    """Minute bars across 2010-11-07 in St. John's, whose clocks went back at 00:01
    to 23:01 the day before: one minute of the 7th, then the 6th again."""
    start = datetime(2010, 11, 6, 12, 0, tzinfo=UTC)
    bars = random_bars(12, 2880)
    return BarSeries([replace(b, ts=start + MINUTE * i) for i, b in enumerate(bars)], 1, "bars")


def test_session_days_where_local_dates_go_back_are_grouped_as_for_a_list() -> None:
    series = _across_a_date_going_back()
    session = Session(start=time(0, 0), end=time(23, 59), timezone="America/St_Johns")
    rng = random.Random(12)
    for _ in range(60):
        end = rng.randrange(1, len(series.bars) + 1)
        window = series.window(rng.randrange(end), end)
        as_of = window[-1].ts + MINUTE
        assert session_days(window, session, as_of) == session_days(list(window), session, as_of)


def test_the_zero_day_range_where_local_dates_go_back_is_measured_as_for_a_list(
    profiles: Profiles,
) -> None:
    series = _across_a_date_going_back()
    prime = replace(
        profiles.instrument.prime_session, start=time(0, 0), timezone="America/St_Johns"
    )
    instrument = replace(
        profiles.instrument,
        prime_session=prime,
        trading_days=frozenset(range(7)),
        holidays=frozenset(),
        no_trade_days=frozenset(),
    )
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, instrument)
    rng = random.Random(13)
    for _ in range(60):
        end = rng.randrange(1, len(series.bars) + 1)
        window = series.window(rng.randrange(end), end)
        cutoff = time(rng.randrange(24), rng.randrange(60))
        assert agent._window_spans(window, cutoff) == agent._spans(list(window), cutoff)


def test_the_zero_day_range_skips_days_that_do_not_count_on_a_window_too(
    profiles: Profiles,
) -> None:
    """Bars every minute through a weekend and a holiday (as a holiday's stub session
    has bars in the data): those days' ranges never count, on a window as for a list."""
    bars = random_bars(14, 8000)
    start = datetime(2024, 3, 28, 7, 0, tzinfo=UTC)  # Thursday; Good Friday is a holiday
    series = BarSeries([replace(b, ts=start + MINUTE * i) for i, b in enumerate(bars)], 1, "b")
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, profiles.instrument)
    whole = series.window(0, len(series.bars))
    for cutoff in (time(5, 0), time(9, 30), time(13, 59)):
        got = agent._window_spans(whole, cutoff)
        assert got == agent._spans(list(whole), cutoff)
        assert date(2024, 3, 29) not in got  # the holiday
        assert date(2024, 3, 30) not in got  # Saturday
        assert date(2024, 4, 1) in got  # Monday counts


def test_the_zero_day_range_on_a_day_whose_clock_goes_back_inside_the_cutoff_hour(
    profiles: Profiles,
) -> None:
    """The autumn change repeats 01:00-01:59: with a cutoff inside that hour the bars
    before it are not a prefix of the day, so the whole day is measured from its bars."""
    bars = random_bars(15, 4000)
    start = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    series = BarSeries([replace(b, ts=start + MINUTE * i) for i, b in enumerate(bars)], 1, "b")
    prime = replace(profiles.instrument.prime_session, start=time(0, 0))
    instrument = replace(
        profiles.instrument,
        prime_session=prime,
        trading_days=frozenset(range(7)),
        holidays=frozenset(),
        no_trade_days=frozenset(),
    )
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, instrument)
    whole = series.window(0, len(series.bars))
    for minute in range(0, 60, 3):
        cutoff = time(1, minute)
        got = agent._window_spans(whole, cutoff)
        assert got == agent._spans(list(whole), cutoff)
        assert date(2026, 11, 1) in got


def _starting_or_ending_inside_the_repeat(at: str) -> BarSeries:
    """St. John's, 2010-11-07: 00:00 of the 7th (02:30 UTC), then the 6th's 23:01 again.
    A series that starts at that minute (the 7th before the 6th) or ends inside the
    repeated hour (the 6th after the 7th): the days' spans are out of order."""
    bars = random_bars(16, 600)
    if at == "start":
        first = datetime(2010, 11, 7, 2, 30, tzinfo=UTC)
    else:
        first = datetime(2010, 11, 7, 2, 45, tzinfo=UTC) - MINUTE * (len(bars) - 1)
    return BarSeries([replace(b, ts=first + MINUTE * i) for i, b in enumerate(bars)], 1, "b")


@pytest.mark.parametrize("at", ["start", "end"])
def test_days_out_of_order_in_a_series_are_grouped_as_for_a_list(
    profiles: Profiles, at: str
) -> None:
    series = _starting_or_ending_inside_the_repeat(at)
    zone = "America/St_Johns"
    session = Session(start=time(0, 0), end=time(23, 59), timezone=zone)
    prime = replace(profiles.instrument.prime_session, start=time(0, 0), timezone=zone)
    instrument = replace(
        profiles.instrument,
        prime_session=prime,
        trading_days=frozenset(range(7)),
        holidays=frozenset(),
        no_trade_days=frozenset(),
    )
    agent = ZeroDayAgent(profiles.strategy.zeroday, profiles.strategy.data, instrument)
    n = len(series.bars)
    for lo, hi in ((0, 1), (0, 2), (0, n), (n - 1, n), (n - 20, n), (1, n), (0, n - 1)):
        window = series.window(lo, hi)
        as_of = window[-1].ts + MINUTE
        assert session_days(window, session, as_of) == session_days(list(window), session, as_of)
        for cutoff in (time(0, 30), time(23, 30), time(23, 59)):
            assert agent._window_spans(window, cutoff) == agent._spans(list(window), cutoff)


def test_one_series_read_with_two_prime_sessions_keeps_each_ones_days(
    profiles: Profiles,
) -> None:
    """The per-series days are kept per prime timezone and start."""
    bars = _sessions(profiles, date(2024, 4, 1), 4, 17)
    series = BarSeries(bars, 1, "bars")
    s, inst = profiles.strategy, profiles.instrument
    agents = [
        ZeroDayAgent(s.zeroday, s.data, inst),
        ZeroDayAgent(
            s.zeroday,
            s.data,
            replace(inst, prime_session=replace(inst.prime_session, start=time(8, 0))),
        ),
        ZeroDayAgent(
            s.zeroday,
            s.data,
            replace(inst, prime_session=replace(inst.prime_session, timezone="Europe/London")),
        ),
    ]
    whole = series.window(0, len(bars))
    for _ in range(2):
        for agent in agents:
            for cutoff in (time(9, 0), time(12, 30)):
                assert agent._window_spans(whole, cutoff) == agent._spans(list(whole), cutoff)


def test_the_levels_closed_minute_memo_answers_as_session_at(
    profiles: Profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every minute across a prime close, a closed hour, a weekend and an early close,
    asked in order and again, also with a memo small enough to start over."""
    from footprint.agents import levels as levels_module
    from footprint.core.calendar import SessionKind, session_at

    s = profiles.strategy
    start = datetime(2024, 11, 27, 20, 0, tzinfo=UTC)  # into Thanksgiving and its half day
    moments = [start + MINUTE * i for i in range(4 * 24 * 60)]
    expected = [session_at(m, profiles.instrument) is SessionKind.CLOSED for m in moments]
    for cap in (100_000, 7):
        monkeypatch.setattr(levels_module, "_CLOSED_MEMO", cap)
        reader = LevelsReader(s.levels, s.trend, s.data, profiles.instrument)
        for _ in range(2):
            assert [reader._closed(m) for m in moments] == expected
    assert any(expected)
    assert not all(expected)


def test_the_levels_snapshot_on_a_window_ending_inside_a_trend_bucket(
    profiles: Profiles,
) -> None:
    """A window that ends a minute or two before a trend bucket closes, read when the
    bucket has closed: the minutes after the window are not present."""
    bars = _sessions(profiles, date(2024, 4, 1), 6, 18)
    series = BarSeries(bars, 1, "bars")
    s = profiles.strategy
    reader = LevelsReader(s.levels, s.trend, s.data, profiles.instrument)
    frame = s.trend.timeframe_minutes
    checked = 0
    for end in range(len(bars) - 600, len(bars) - 300):
        closes = bars[end].ts + MINUTE  # the minute after the window's next bar
        if (closes - datetime(1970, 1, 1, tzinfo=UTC)) % (MINUTE * frame):
            continue  # the window's next bar is not a bucket's last minute
        window = series.window(max(0, end - 43_200), end - 1)  # two minutes short
        got = reader.snapshot(window, closes)
        assert got == reader.snapshot(list(window), closes)
        checked += 1
    assert checked


# --- review of PR 49 (certified with conditions): F1, F3, F4 -------------------------


def _last_session_spike(profiles: Profiles) -> BarSeries:
    """25 sessions; the series' last bar (13:59 PT, the day's last bar) is the day's
    extreme: its high far above everything before it."""
    bars = _sessions(profiles, date(2024, 4, 1), 25, 19)
    last = bars[-1]
    bars[-1] = replace(last, high=last.high + D(500))
    return BarSeries(bars, 1, "bars")


def test_a_window_one_bar_short_of_a_days_last_bar_never_sees_it(profiles: Profiles) -> None:
    """F1: the window ends one bar before the day's last bar (the day's high). Read with
    a cutoff after that bar, the day is cut by the window, so it is measured from the
    window's own bars: the extreme outside the window never counts."""
    series = _last_session_spike(profiles)
    n = len(series.bars)
    window = series.window(0, n - 1)
    s = profiles.strategy
    agent = ZeroDayAgent(s.zeroday, s.data, profiles.instrument)
    today = series.bars[-1].ts.astimezone(PT).date()
    for cutoff in (time(13, 59, 30), time(23, 0)):
        got = agent._window_spans(window, cutoff)
        assert got == agent._spans(list(window), cutoff)
        assert got[today] < D(500)
    # Through the public check: a mid-minute moment, after the left-out bar opened.
    as_of = series.bars[-1].ts + timedelta(seconds=30)
    assert agent.check(window, as_of) == agent.check(list(window), as_of)
    # The left-out bar would change today's range if it were read (it is the extreme).
    with_it = agent._spans(list(series.bars), time(13, 59, 30))[today]
    assert with_it != agent._spans(list(window), time(13, 59, 30))[today]


def test_two_series_alive_keep_their_own_clock_index() -> None:
    """F3 (R1): the clock index is per series, never one series' reused for another."""
    first = BarSeries(random_bars(20, 3000), 1, "bars")
    other_bars = random_bars(21, 3000)
    second = BarSeries(other_bars, 1, "bars")
    rng = random.Random(20)
    for series in (first, second, first, second):
        window = series.window(0, len(series.bars))
        for _ in range(40):
            index = rng.randrange(len(window))
            assert same_time_indices(window, index, 3, TZ) == same_time_indices(
                list(window), index, 3, TZ
            )


def test_two_levels_readers_on_two_instruments_keep_their_own_closed_minutes(
    profiles: Profiles,
) -> None:
    """F3 (R5): the closed-minute memo belongs to one reader and its instrument."""
    from footprint.core.calendar import SessionKind, session_at

    s = profiles.strategy
    nq = profiles.instrument
    es_style = replace(nq, prime_session=replace(nq.prime_session, end=time(13, 0)))
    start = datetime(2024, 4, 2, 19, 0, tzinfo=UTC)  # 12:00 PT
    moments = [start + MINUTE * i for i in range(240)]
    for instrument in (nq, es_style, nq):
        reader = LevelsReader(s.levels, s.trend, s.data, instrument)
        expected = [session_at(m, instrument) is SessionKind.CLOSED for m in moments]
        assert [reader._closed(m) for m in moments] == expected
    other = LevelsReader(s.levels, s.trend, s.data, es_style)
    assert [other._closed(m) for m in moments] != [
        session_at(m, nq) is SessionKind.CLOSED for m in moments
    ]


def test_a_series_and_its_windows_are_read_only() -> None:
    """F4: a checked series cannot be swapped or edited under its windows."""
    series = BarSeries(random_bars(22, 50), 1, "bars")
    window = series.window(5, 20)
    assert isinstance(series.starts, tuple)
    with pytest.raises(AttributeError):
        series.bars = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        series.starts = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        series.timeframe_minutes = 15  # type: ignore[misc]
    for name, value in (("start", 0), ("series", series), ("other", 1)):
        with pytest.raises(AttributeError):
            setattr(window, name, value)
    with pytest.raises(AttributeError):
        del window.start
    assert (window.start, window.end, window.series) == (5, 20, series)


def test_the_range_days_of_a_window_starting_after_todays_cash_close_read_only_its_bars(
    profiles: Profiles,
) -> None:
    """LOW-2 (reviewer's R8). St. John's, 2010-11-06, with a cash session of 22:00-23:30:
    the clock goes back at 00:01 of the 7th to 23:01 of the 6th, so the 6th's 23:01-23:59
    comes twice. A window starting at the second 23:10 holds a cash session of the 6th
    (23:10-23:29) while the first pass's after-close bars of the 6th (23:30-23:59) lie
    before the window. The range days read from the window only, as for the list of its
    bars: those earlier after-close bars are never searched."""
    series = _across_a_date_going_back()
    cash = Session(start=time(22, 0), end=time(23, 30), timezone="America/St_Johns")
    pipeline = Pipeline(
        replace(profiles, instrument=replace(profiles.instrument, cash_session=cash))
    )
    first_after = series.starts.index(datetime(2010, 11, 7, 2, 0, tzinfo=UTC))  # 23:30 NDT
    start = series.starts.index(datetime(2010, 11, 7, 2, 40, tzinfo=UTC))  # 23:10 NST
    as_of = datetime(2010, 11, 7, 3, 15, tzinfo=UTC)  # 23:45 NST on the 6th, after the close
    end = series.starts.index(as_of)
    window = series.window(start, end)
    earlier = series.bars[first_after:start]
    assert len(earlier) == 40  # the first 23:30-23:59, 00:00, then the second 23:01-23:09
    for b in earlier[:30]:  # today's after-close bars, before the window
        assert local_time(b.ts, cash.timezone).date() == date(2010, 11, 6)
        assert local_time(b.ts, cash.timezone).time() >= cash.end
    got = pipeline._range_days(window, as_of)
    assert got is not None
    assert got == pipeline._range_days(list(window), as_of)
    today = got[-1]
    in_window = [b for b in window if b.ts >= datetime(2010, 11, 7, 2, 40, tzinfo=UTC)]
    assert today.volume == sum(b.volume for b in in_window)  # nothing from before the window


def _helper_results(window: tuple[Bar, ...], profiles: Profiles) -> list[object]:
    """Every series helper's answer on `window` at its last bar's close."""
    as_of = window[-1].ts + MINUTE
    s = profiles.strategy
    cash = profiles.instrument.cash_session
    return [
        tuple(window),
        completed(window, as_of, 1),
        resample(window, 15, as_of),
        session_days(window, cash, as_of),
        ZeroDayAgent(s.zeroday, s.data, profiles.instrument).check(window, as_of),
        LevelsReader(s.levels, s.trend, s.data, profiles.instrument).snapshot(window, as_of),
        Pipeline(profiles)._range_days(window, as_of),
    ]


def test_a_window_survives_copy_deepcopy_and_pickle(profiles: Profiles) -> None:
    """LOW-1: a window copies, deep-copies and pickles to an equal window of an equal
    series, and every helper answers the copy as it answers the original."""
    series = BarSeries(_sessions(profiles, date(2024, 3, 4), 3, 0), 1, "bars")
    window = series.window(200, len(series.bars) - 50)
    expected = _helper_results(window, profiles)
    shallow = copy.copy(window)
    deep = copy.deepcopy(window)
    pickled = pickle.loads(pickle.dumps(window))
    assert shallow.series is series  # a shallow copy shares the series
    for twin in (shallow, deep, pickled):
        assert type(twin) is BarWindow
        assert twin == window
        assert (twin.start, twin.end) == (window.start, window.end)
        assert twin.series.bars == series.bars
        assert twin.series.starts == series.starts
        assert twin.series.timeframe_minutes == series.timeframe_minutes
        assert _helper_results(twin, profiles) == expected
        with pytest.raises(AttributeError):
            twin.start = 0
    for holder in ((window,), [window], {"w": window}):
        assert copy.deepcopy(holder) == holder
        assert pickle.loads(pickle.dumps(holder)) == holder


def test_a_dataclass_holding_a_window_cannot_go_through_asdict() -> None:
    """Out of scope (LOW-1): `dataclasses.asdict` rebuilds a tuple subclass as
    type(obj)(converted items), and a window's items would be dicts, not bars of a
    series, so no window can be made of them. Pinned so a change is seen; nothing in
    src keeps a window in a journal or readout dataclass."""

    @dataclass(frozen=True)
    class Holder:
        bars: tuple[Bar, ...]

    window = BarSeries(random_bars(23, 40), 1, "bars").window(3, 30)
    with pytest.raises(TypeError):
        asdict(Holder(window))
    assert asdict(Holder(tuple(window)))["bars"][0]["ts"] == window[0].ts
