from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from footprint.agents._series import ema, relative_volume, require_series
from footprint.agents.momentum_reader import MomentumReader
from footprint.agents.range_reader import RangeReader
from footprint.agents.trend_reader import TrendReader
from footprint.agents.volume_reader import VolumeReader
from footprint.core.config import InstrumentProfile, StrategyProfile
from footprint.core.interfaces import Reader
from footprint.core.models import Bar, Score
from tests.agents.conftest import DAY0_0630, bar, series

DAY = timedelta(days=1)
MIN = timedelta(minutes=1)


def same_time_history(days: int, volume: int) -> list[Bar]:
    """One 1-minute bar at 06:30 PT on each of `days` consecutive days."""
    return [bar(DAY0_0630 + DAY * i, 100, 101, 99, 100, volume) for i in range(days)]


# --- shared helpers -----------------------------------------------------------


def test_every_reader_satisfies_the_reader_protocol(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    readers: list[Reader] = [
        TrendReader(strategy.trend),
        VolumeReader(strategy.volume, instrument),
        MomentumReader(strategy.volume, instrument),
        RangeReader(strategy.range),
    ]
    assert all(isinstance(r, Reader) for r in readers)
    assert [r.agent_id for r in readers] == ["trend", "volume", "momentum", "range"]


def test_require_series_rejects_bad_input() -> None:
    a = bar(DAY0_0630, 100, 101, 99, 100)
    with pytest.raises(ValueError, match="empty"):
        require_series([], 1, "x")
    with pytest.raises(ValueError, match="mixes symbols"):
        require_series([a, replace(a, symbol="ES", ts=a.ts + MIN)], 1, "x")
    with pytest.raises(ValueError, match="increasing"):
        require_series([a, a], 1, "x")
    with pytest.raises(ValueError, match="multiple of 15"):
        require_series([a, replace(a, ts=a.ts + MIN)], 15, "x")
    with pytest.raises(TypeError):
        require_series([a, "bar"], 1, "x")  # type: ignore[list-item]


def test_ema_matches_hand_calculation() -> None:
    values = [Decimal(v) for v in (1, 2, 3, 4)]
    # period 2: seed (1+2)/2 = 1.5; alpha 2/3 -> 1.5+2/3*(3-1.5)=2.5 -> 2.5+2/3*(4-2.5)=3.5
    assert ema(values, 2) == [Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]
    assert ema(values, 5) == []


def test_relative_volume_uses_only_same_time_of_day_bars(instrument: InstrumentProfile) -> None:
    tz = instrument.prime_session.timezone
    history = same_time_history(20, 100)
    noise = [bar(DAY0_0630 + DAY * i + MIN, 100, 101, 99, 100, 99999) for i in range(20)]
    bars = sorted([*history, *noise], key=lambda b: b.ts)
    bars.append(bar(DAY0_0630 + DAY * 20, 100, 101, 99, 100, 300))
    assert relative_volume(bars, len(bars) - 1, 20, tz) == Decimal(3)
    assert relative_volume(bars[-10:], 9, 20, tz) is None  # too little history


# --- Nadia (range) ------------------------------------------------------------


def days_with_ranges(history_range: int, today_range: int, n: int = 10) -> list[Bar]:
    days = [bar(DAY0_0630 + DAY * i, 1000, 1000 + history_range, 1000, 1000) for i in range(n)]
    days.append(bar(DAY0_0630 + DAY * n, 1000, 1000 + today_range, 1000, 1000))
    return days


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (69, Score.BULLISH),  # under 70% of ADR: room to run
        (70, Score.NEUTRAL),  # exactly 70%: neutral band
        (100, Score.NEUTRAL),  # exactly 100%: neutral band
        (101, Score.BEARISH),  # over 100%: extended
    ],
)
def test_range_reader_bands(strategy: StrategyProfile, today: int, expected: Score) -> None:
    read = RangeReader(strategy.range).read(days_with_ranges(100, today))
    assert read.score is expected


def test_range_reader_needs_full_history(strategy: StrategyProfile) -> None:
    read = RangeReader(strategy.range).read(days_with_ranges(100, 10, n=9))
    assert read.score is Score.NEUTRAL
    assert "insufficient" in read.reason


def test_range_reader_zero_adr_is_neutral(strategy: StrategyProfile) -> None:
    assert RangeReader(strategy.range).read(days_with_ranges(0, 5)).score is Score.NEUTRAL


# --- Rafael (volume) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("volume", "o", "c", "expected"),
    [
        (300, 100, 101, Score.BULLISH),  # 3.0x spike, bullish candle
        (300, 101, 100, Score.BEARISH),  # 3.0x spike, bearish candle
        (300, 100, 100, Score.NEUTRAL),  # spike on a doji: no direction
        (299, 100, 101, Score.NEUTRAL),  # 2.99x: below the spike multiple
    ],
)
def test_volume_reader(
    strategy: StrategyProfile,
    instrument: InstrumentProfile,
    volume: int,
    o: float,
    c: float,
    expected: Score,
) -> None:
    bars = same_time_history(20, 100)
    bars.append(bar(DAY0_0630 + DAY * 20, o, 102, 98, c, volume))
    assert VolumeReader(strategy.volume, instrument).read(bars).score is expected


def test_volume_reader_without_history_never_signals(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    bars = [bar(DAY0_0630, 100, 102, 98, 101, 10**6)]
    read = VolumeReader(strategy.volume, instrument).read(bars)
    assert read.score is Score.NEUTRAL
    assert "cannot measure" in read.reason


def test_volume_reader_rejects_wrong_timeframe(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    bars = [bar(DAY0_0630, 100, 101, 99, 100), bar(DAY0_0630 + timedelta(seconds=30), 1, 2, 1, 1)]
    with pytest.raises(ValueError, match="multiple of"):
        VolumeReader(strategy.volume, instrument).read(bars)


# --- Darius (momentum) --------------------------------------------------------


def momentum_bars(spike_volume: int, spike: tuple[float, float], follow_close: float) -> list[Bar]:
    bars = same_time_history(20, 100)
    spike_open, spike_close = spike
    lo, hi = min(spike_open, spike_close) - 1, max(spike_open, spike_close) + 1
    bars.append(bar(DAY0_0630 + DAY * 20, spike_open, hi, lo, spike_close, spike_volume))
    f_lo, f_hi = min(spike_close, follow_close) - 1, max(spike_close, follow_close) + 1
    bars.append(bar(DAY0_0630 + DAY * 20 + MIN, spike_close, f_hi, f_lo, follow_close))
    return bars


@pytest.mark.parametrize(
    ("spike_volume", "spike", "follow", "expected"),
    [
        (300, (100, 104), 105, Score.BULLISH),  # spike up, follow-through up
        (300, (104, 100), 99, Score.BEARISH),  # spike down, follow-through down
        (300, (100, 104), 104, Score.NEUTRAL),  # follow closes level: no follow-through
        (300, (100, 104), 103, Score.NEUTRAL),  # follow closes back inside
        (299, (100, 104), 105, Score.NEUTRAL),  # not a spike
        (300, (100, 100), 105, Score.NEUTRAL),  # spike on a doji
    ],
)
def test_momentum_reader(
    strategy: StrategyProfile,
    instrument: InstrumentProfile,
    spike_volume: int,
    spike: tuple[float, float],
    follow: float,
    expected: Score,
) -> None:
    bars = momentum_bars(spike_volume, spike, follow)
    assert MomentumReader(strategy.volume, instrument).read(bars).score is expected


def test_momentum_reader_needs_two_bars(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    read = MomentumReader(strategy.volume, instrument).read([bar(DAY0_0630, 1, 2, 1, 2)])
    assert read.score is Score.NEUTRAL


def test_momentum_read_is_stamped_at_the_follow_through_bar(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    bars = momentum_bars(300, (100, 104), 105)
    assert MomentumReader(strategy.volume, instrument).read(bars).ts == bars[-1].ts


# --- Mateo (trend) ------------------------------------------------------------


def zigzag(start: float, legs: int, up: float, down: float) -> list[float]:
    """Up 3 bars by `up` each, down 2 bars by `down` each, repeated; ends on an up leg."""
    mids, price = [start], start
    for _ in range(legs):
        for _ in range(3):
            price += up
            mids.append(price)
        for _ in range(2):
            price -= down
            mids.append(price)
    for _ in range(3):
        price += up
        mids.append(price)
    return mids


def test_trend_reader_uptrend(strategy: StrategyProfile) -> None:
    bars = series(DAY0_0630, 15, zigzag(1000, 8, up=4, down=3))
    assert TrendReader(strategy.trend).read(bars).score is Score.BULLISH


def test_trend_reader_downtrend(strategy: StrategyProfile) -> None:
    mids = [2000 - (m - 1000) for m in zigzag(1000, 8, up=4, down=3)]
    assert TrendReader(strategy.trend).read(series(DAY0_0630, 15, mids)).score is Score.BEARISH


def test_trend_reader_flat_market_is_neutral(strategy: StrategyProfile) -> None:
    mids = [1000.0 + (3 if i % 4 < 2 else -3) for i in range(40)]
    assert TrendReader(strategy.trend).read(series(DAY0_0630, 15, mids)).score is Score.NEUTRAL


def test_trend_reader_uptrend_but_close_below_fast_ema_is_neutral(
    strategy: StrategyProfile,
) -> None:
    mids = zigzag(1000, 8, up=4, down=3)
    mids.append(mids[-1] - 40)  # last bar collapses below the EMAs
    assert TrendReader(strategy.trend).read(series(DAY0_0630, 15, mids)).score is Score.NEUTRAL


def test_trend_reader_insufficient_bars(strategy: StrategyProfile) -> None:
    read = TrendReader(strategy.trend).read(series(DAY0_0630, 15, [1000.0] * 10))
    assert read.score is Score.NEUTRAL
    assert "insufficient" in read.reason


def test_trend_reader_refuses_one_minute_bars(strategy: StrategyProfile) -> None:
    with pytest.raises(ValueError, match="multiple of 15"):
        TrendReader(strategy.trend).read(series(DAY0_0630, 1, zigzag(1000, 8, 4, 3)))


def test_momentum_doji_spike_says_why(
    strategy: StrategyProfile, instrument: InstrumentProfile
) -> None:
    read = MomentumReader(strategy.volume, instrument).read(momentum_bars(300, (100, 100), 95))
    assert read.score is Score.NEUTRAL
    assert "doji" in read.reason
