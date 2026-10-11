"""Q5.3 slice 2b-1: the pullback detector fed by the spike test, as an EntryTrigger
(the momentum slot of the decision pipeline), backtest rule library only.

The spike test is "the same spike test as breakout" read as the spike bar test alone
(MomentumReader step 1: relative volume of the bar at or above the configured multiple
on a candle with a direction), pinned here against MomentumReader and VolumeReader.
The trigger answers for the LAST bar it is handed and must give exactly the answer a
scan of the whole history gives. Synthetic bars say NOTHING about profitability.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from footprint.agents.momentum_reader import MomentumReader
from footprint.agents.volume_reader import VolumeReader
from footprint.backtest.pullback import (
    PullbackSettings,
    PullbackTrigger,
    pullback_entries,
    spike_score,
)
from footprint.core.config import InstrumentProfile, VolumeSettings, load_instrument
from footprint.core.models import Bar, Score
from tests.backtest.test_pullback import BASE, MINUTE, T0, mirror
from tests.conftest import REPO_ROOT

INSTRUMENT: InstrumentProfile = load_instrument(REPO_ROOT / "config" / "instruments" / "nq.yaml")
TZ = INSTRUMENT.prime_session.timezone
VOL = VolumeSettings(timeframe_minutes=1, lookback_sessions=1, spike_multiple=Decimal(3))
LIFE = PullbackSettings(lifetime_minutes=30)
FLAT = ("100", "100.5", "99.5", "100")
ENTRY = 3  # BASE's entry bar (today's index)


def _bar(ts: datetime, row: tuple[str, str, str, str], volume: int) -> Bar:
    o, h, lo, c = row
    return Bar("NQ", ts, Decimal(o), Decimal(h), Decimal(lo), Decimal(c), volume)


def day(rows: list[tuple[str, str, str, str]], volumes: list[int]) -> list[Bar]:
    """Yesterday at the same clock times (flat, volume 100, the relative-volume
    baseline), then today's rows with their volumes. The overnight gap is a missing
    bar, so nothing carries over from yesterday."""
    yesterday = [_bar(T0 - timedelta(days=1) + i * MINUTE, FLAT, 100) for i in range(len(rows))]
    today = [
        _bar(T0 + i * MINUTE, row, v) for i, (row, v) in enumerate(zip(rows, volumes, strict=True))
    ]
    return yesterday + today


SPIKE_VOLUMES = [300, 100, 100, 100, 100]  # bar 0: exactly 3x yesterday's same-time bar


def trigger(settings: PullbackSettings = LIFE) -> PullbackTrigger:
    return PullbackTrigger(settings, VOL, TZ, MINUTE)


def scores(bars: list[Bar]) -> list[Score]:
    return [spike_score(bars, i, VOL, TZ) for i in range(len(bars))]


def test_the_spike_test_is_the_volume_readers_read_of_that_bar() -> None:
    rows = [BASE[0], BASE[1], ("100", "101", "99", "100"), mirror(BASE)[0], BASE[4]]
    bars = day(rows, [300, 299, 900, 450, 100])  # 3x up, under 3x, doji, 4.5x down, none
    got = scores(bars)
    assert got[len(rows) :] == [
        Score.BULLISH,
        Score.NEUTRAL,
        Score.NEUTRAL,
        Score.BEARISH,
        Score.NEUTRAL,
    ]
    reader = VolumeReader(VOL, INSTRUMENT)
    for i in range(len(bars)):
        assert got[i] is reader.read(bars[: i + 1]).score, i


def test_the_spike_test_is_momentum_reader_step_one() -> None:
    # Whenever breakout fires on bar i+1, the spike test marks bar i in that direction;
    # and a marked bar followed by a bar closing beyond it makes breakout fire.
    bars = day(BASE, SPIKE_VOLUMES)
    down = day(mirror(BASE), SPIKE_VOLUMES)
    reader = MomentumReader(VOL, INSTRUMENT)
    for series in (bars, down):
        marks = scores(series)
        for i in range(len(series) - 1):
            read = reader.read(series[: i + 2]).score
            if read is not Score.NEUTRAL:
                assert marks[i] is read, i
        spike = len(BASE)  # today's bar 0
        assert marks[spike] is not Score.NEUTRAL
        assert reader.read(series[: spike + 2]).score is marks[spike]  # bar 1 follows through


def test_reads_bullish_only_on_the_up_entry_bar() -> None:
    bars = day(BASE, SPIKE_VOLUMES)
    today = len(BASE)
    for end in range(1, len(bars) + 1):
        read = trigger().read(bars[:end])
        assert read.ts == bars[end - 1].ts
        assert read.agent_id == "momentum"
        assert read.score is (Score.BULLISH if end - 1 == today + ENTRY else Score.NEUTRAL), end
    assert trigger().read(bars[: today + ENTRY + 1]).reason == "pullback: up pullback entry"
    assert trigger().read(bars[: today + ENTRY]).reason == "pullback: no pullback entry"


def test_reads_bearish_only_on_the_down_entry_bar() -> None:
    bars = day(mirror(BASE), SPIKE_VOLUMES)
    today = len(BASE)
    for end in range(1, len(bars) + 1):
        read = trigger().read(bars[:end])
        assert read.score is (Score.BEARISH if end - 1 == today + ENTRY else Score.NEUTRAL), end
    assert trigger().read(bars[: today + ENTRY + 1]).reason == "pullback: down pullback entry"


def test_no_volume_spike_no_entry() -> None:
    for rows in (BASE, mirror(BASE)):
        bars = day(rows, [299, 100, 100, 100, 100])
        assert trigger().read(bars[: len(BASE) + ENTRY + 1]).score is Score.NEUTRAL


@pytest.mark.parametrize(("minutes", "fires"), [(ENTRY, True), (ENTRY - 1, False)])
def test_the_configured_lifetime_bounds_the_setup(minutes: int, fires: bool) -> None:
    # The entry bar is ENTRY minutes after the spike bar: alive at exactly that lifetime.
    settings = replace(LIFE, lifetime_minutes=minutes)
    for rows, score in ((BASE, Score.BULLISH), (mirror(BASE), Score.BEARISH)):
        bars = day(rows, SPIKE_VOLUMES)
        read = trigger(settings).read(bars[: len(BASE) + ENTRY + 1])
        assert read.score is (score if fires else Score.NEUTRAL)


def test_the_window_gives_the_whole_history_answer() -> None:
    # Two spikes, a later one inside the first one's lifetime, a gap, and a third spike:
    # on every prefix the trigger equals a scan of the whole prefix.
    rows = BASE + BASE + [FLAT] + BASE
    vols = SPIKE_VOLUMES + SPIKE_VOLUMES + [100] + SPIKE_VOLUMES
    for life in (1, 2, 3, 4, 7, 30):
        settings = PullbackSettings(lifetime_minutes=life)
        for source in (rows, [mirror([r])[0] for r in rows]):
            bars = day(source, vols)
            gap = len(rows) + 2 * len(BASE)
            bars = bars[:gap] + [replace(b, ts=b.ts + 2 * MINUTE) for b in bars[gap:]]
            marks = scores(bars)
            for end in range(1, len(bars) + 1):
                up = pullback_entries(
                    bars[:end], marks[:end], settings.lifetime, MINUTE, rising=True
                )
                down = pullback_entries(
                    bars[:end], marks[:end], settings.lifetime, MINUTE, rising=False
                )
                expected = (
                    Score.BULLISH
                    if end - 1 in up
                    else Score.BEARISH
                    if end - 1 in down
                    else Score.NEUTRAL
                )
                assert trigger(settings).read(bars[:end]).score is expected, (life, end)
