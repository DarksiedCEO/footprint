"""The play caller's regime reader (config/regime.yaml; pre-registration DRAFT 2026-10-21).

Written before the code. The reader labels a trading day TREND, CHOP or STAND_ASIDE at
`decision_minutes` after the cash open, from completed 1-minute bars only:
- opening efficiency = |last close - first open| / (sum of |close-to-close| moves,
  starting from the first bar's open) over [cash open, cash open + decision_minutes);
- its rank = the percent of the previous `efficiency_lookback_sessions` trading days
  whose efficiency is strictly lower;
- overnight range = high - low over [overnight open, cash open), compared with the
  median of the previous `overnight_lookback_sessions` trading days.
TREND: rank >= trend_rank_pct AND overnight range >= that median. CHOP: rank <
chop_rank_pct. Anything else, and any missing or unusable data: STAND_ASIDE.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.regime_reader import (
    Regime,
    RegimeCall,
    RegimeReader,
    classify,
    opening_efficiency,
)
from footprint.core.calendar import is_trading_day, previous_trading_day
from footprint.core.config import (
    ConfigError,
    DataSettings,
    InstrumentProfile,
    RegimeSettings,
    Session,
    StrategyProfile,
    load_regime,
)
from footprint.core.models import Bar
from tests.agents.conftest import bar
from tests.conftest import REPO_ROOT

PT = ZoneInfo("America/Los_Angeles")
MINUTE = timedelta(minutes=1)
SMALL = RegimeSettings(
    decision_minutes=30,
    efficiency_lookback_sessions=6,
    trend_rank_pct=Decimal(67),
    chop_rank_pct=Decimal(33),
    overnight_lookback_sessions=4,
)
TODAY = date(2026, 10, 14)  # a Wednesday


@pytest.fixture
def data(strategy: StrategyProfile) -> DataSettings:
    return strategy.data


def _cash_open(day: date, inst: InstrumentProfile) -> datetime:
    zone = ZoneInfo(inst.cash_session.timezone)
    return datetime.combine(day, inst.cash_session.start, zone).astimezone(UTC)


def _night_open(day: date, inst: InstrumentProfile) -> datetime:
    night = inst.overnight_session
    start_day = day - timedelta(days=1) if night.start > night.end else day
    return datetime.combine(start_day, night.start, ZoneInfo(night.timezone)).astimezone(UTC)


def _closes_for(efficiency: str) -> list[float]:
    """30 closes from an open of 100: 'clean' one way (ER 1), 'chop' back and forth
    (ER 1/30), 'mid' (ER 0.5: 20 up, 10 down... computed in the test that needs it)."""
    if efficiency == "clean":
        return [100.0 + (i + 1) for i in range(30)]
    if efficiency == "chop":
        return [101.0 if i % 2 == 0 else 100.0 for i in range(29)] + [101.0]
    if efficiency == "flat":
        return [100.0] * 30
    raise ValueError(efficiency)


def _day_bars(
    day: date,
    inst: InstrumentProfile,
    *,
    closes: Sequence[float],
    night_range: float,
    skip: frozenset[int] = frozenset(),
    night_begin: datetime | None = None,
) -> list[Bar]:
    out: list[Bar] = []
    ts = night_begin or _night_open(day, inst)
    open_ = _cash_open(day, inst)
    half = night_range / 2
    while ts < open_:
        out.append(bar(ts, 100.0, 100.0 + half, 100.0 - half, 100.0))
        ts += MINUTE
    previous = 100.0
    for i, close in enumerate(closes):
        if i not in skip:
            hi, lo = max(previous, close), min(previous, close)
            out.append(bar(open_ + MINUTE * i, previous, hi, lo, close))
        previous = close
    return out


def _history(n: int, inst: InstrumentProfile, today: date = TODAY) -> list[date]:
    days: list[date] = []
    day = today
    while len(days) < n:
        day = previous_trading_day(day, inst)
        days.append(day)
    return days[::-1]


def _build(
    inst: InstrumentProfile,
    *,
    today_closes: Sequence[float],
    today_night: float,
    history_closes: Sequence[Sequence[float]] | None = None,
    history_nights: Sequence[float] | None = None,
    today: date = TODAY,
) -> list[Bar]:
    n = SMALL.efficiency_lookback_sessions
    days = _history(n, inst, today)
    h_closes = history_closes or [_mid_closes(k) for k in range(n)]
    h_nights = history_nights or [10.0] * n
    bars: list[Bar] = []
    for day, closes, night in zip(days, h_closes, h_nights, strict=True):
        bars += _day_bars(day, inst, closes=closes, night_range=night)
    bars += _day_bars(today, inst, closes=today_closes, night_range=today_night)
    return bars


def _mid_closes(k: int) -> list[float]:
    """History day k (0..5): 20 + k one-point steps up, then 10 - k steps down: net
    10 + 2k over a path of 30, efficiency (10 + 2k) / 30. All six differ and sit
    strictly between a chop opening (1/30) and a clean one (1)."""
    ups = 20 + k
    downs = 30 - ups if 30 - ups > 0 else 0
    steps = [1.0] * ups + [-1.0] * downs
    closes, level = [], 100.0
    for step in steps[:30]:
        level += step
        closes.append(level)
    while len(closes) < 30:
        closes.append(level)
    return closes


def _efficiency(closes: Sequence[float]) -> Decimal:
    path = Decimal(0)
    previous = Decimal(100)
    for close in closes:
        c = Decimal(str(close))
        path += abs(c - previous)
        previous = c
    return abs(Decimal(str(closes[-1])) - Decimal(100)) / path


def _decision(inst: InstrumentProfile, day: date = TODAY) -> datetime:
    return _cash_open(day, inst) + MINUTE * SMALL.decision_minutes


def _reader(inst: InstrumentProfile, data: DataSettings, s: RegimeSettings = SMALL) -> RegimeReader:
    return RegimeReader(s, data, inst)


# --- the config ----------------------------------------------------------------


def test_repo_config_loads() -> None:
    s = load_regime(REPO_ROOT / "config" / "regime.yaml")
    assert s.decision_minutes >= 1
    assert s.chop_rank_pct < s.trend_rank_pct


def _write(tmp_path: Path, **over: object) -> Path:
    raw: dict[str, object] = {
        "decision_minutes": 30,
        "efficiency_lookback_sessions": 60,
        "trend_rank_pct": 67,
        "chop_rank_pct": 33,
        "overnight_lookback_sessions": 20,
    }
    raw.update(over)
    path = tmp_path / "regime.yaml"
    path.write_text("".join(f"{k}: {v}\n" for k, v in raw.items() if v is not None))
    return path


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"chop_rank_pct": 67}, "chop_rank_pct must be < trend_rank_pct"),
        ({"chop_rank_pct": 70}, "chop_rank_pct must be < trend_rank_pct"),
        ({"trend_rank_pct": 101}, "trend_rank_pct must be <= 100"),
        ({"decision_minutes": 0}, "decision_minutes must be >= 1"),
        ({"efficiency_lookback_sessions": 0}, "efficiency_lookback_sessions must be >= 1"),
        ({"overnight_lookback_sessions": 0}, "overnight_lookback_sessions must be >= 1"),
        ({"chop_rank_pct": 0}, "chop_rank_pct must be a finite number > 0"),
        ({"trend_rank_pct": "true"}, "trend_rank_pct must be a number"),
        ({"extra_key": 1}, "unknown keys"),
        ({"decision_minutes": None}, "missing keys"),
    ],
)
def test_bad_config_is_refused(tmp_path: Path, over: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_regime(_write(tmp_path, **over))


# --- labels ----------------------------------------------------------------------


def test_clean_open_after_a_wide_night_is_trend(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.TREND, call.reason
    assert call.day == TODAY
    assert call.efficiency == Decimal(1)
    assert call.efficiency_rank_pct == Decimal(100)
    assert call.overnight_range == Decimal(20)
    assert call.overnight_median == Decimal(10)


def test_clean_open_after_a_narrow_night_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=5.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "overnight" in call.reason


def test_overnight_exactly_at_the_median_is_enough(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=10.0)
    assert _reader(instrument, data).call(bars, _decision(instrument)).regime is Regime.TREND


def test_overnight_median_uses_only_the_overnight_lookback(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Oldest first. Only the newest 4 count: [60, 20, 10, 10], median 15. Their mean
    # is 25, the newest five's median 20 and all six's 40: today's night (15) is a
    # TREND only with the right window and the median.
    nights = [150.0, 150.0, 60.0, 20.0, 10.0, 10.0]
    bars = _build(
        instrument, today_closes=_closes_for("clean"), today_night=15.0, history_nights=nights
    )
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.overnight_median == Decimal(15)
    assert call.regime is Regime.TREND


def test_choppy_open_is_chop_whatever_the_night(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("chop"), today_night=50.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.CHOP, call.reason
    assert call.efficiency_rank_pct == Decimal(0)
    assert call.efficiency == _efficiency(_closes_for("chop"))


def _rank_case(rank_below: int) -> tuple[list[list[float]], list[float]]:
    """History with 6 distinct efficiencies (k = 0..5 -> rising); today's closes give
    an efficiency strictly above exactly `rank_below` of them."""
    history = [_mid_closes(k) for k in range(6)]
    effs = sorted(_efficiency(c) for c in history)
    # today = the k-th history day's own path: equal to it, so strictly above k of them.
    for k in range(6):
        if _efficiency(_mid_closes(k)) == effs[rank_below]:
            return history, _mid_closes(k)
    raise AssertionError("no matching history day")


@pytest.mark.parametrize(
    ("below", "regime"),
    [
        # rank = 100 * below / 6: 0 -> 0, 1 -> 16.7, 2 -> 33.3, 3 -> 50, 4 -> 66.7, 5 -> 83.3
        (0, Regime.CHOP),
        (1, Regime.CHOP),
        (2, Regime.STAND_ASIDE),  # 33.3 is not below 33... it is above: not CHOP
        (3, Regime.STAND_ASIDE),
        (4, Regime.STAND_ASIDE),  # 66.7 < 67: not TREND
        (5, Regime.TREND),
    ],
)
def test_rank_thresholds(
    instrument: InstrumentProfile, data: DataSettings, below: int, regime: Regime
) -> None:
    history, today = _rank_case(below)
    bars = _build(instrument, today_closes=today, today_night=20.0, history_closes=history)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.efficiency_rank_pct == Decimal(100) * below / 6
    assert call.regime is regime, call.reason


def test_rank_exactly_at_the_trend_threshold_is_trend(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    history, today = _rank_case(4)
    s = replace(SMALL, trend_rank_pct=Decimal(100) * 4 / 6)
    bars = _build(instrument, today_closes=today, today_night=20.0, history_closes=history)
    assert _reader(instrument, data, s).call(bars, _decision(instrument)).regime is Regime.TREND


def test_rank_exactly_at_the_chop_threshold_is_not_chop(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    history, today = _rank_case(2)
    s = replace(SMALL, chop_rank_pct=Decimal(100) * 2 / 6)
    bars = _build(instrument, today_closes=today, today_night=20.0, history_closes=history)
    call = _reader(instrument, data, s).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE


def test_equal_efficiency_does_not_count_as_lower(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    history = [_closes_for("clean")] * 6
    bars = _build(
        instrument, today_closes=_closes_for("clean"), today_night=20.0, history_closes=history
    )
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.efficiency_rank_pct == Decimal(0)
    assert call.regime is Regime.CHOP


# --- timing and no lookahead -----------------------------------------------------


def test_before_the_decision_time_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument) - MINUTE)
    assert call.regime is Regime.STAND_ASIDE
    assert "before" in call.reason


def test_later_in_the_day_the_label_is_unchanged(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    # Bars after the decision window (a reversal) must not change the label.
    late = _decision(instrument)
    bars += [bar(late + MINUTE * i, 130.0, 130.0, 50.0, 50.0) for i in range(60)]
    reader = _reader(instrument, data)
    at_decision = reader.call(bars, _decision(instrument))
    later = reader.call(bars, _decision(instrument) + timedelta(hours=1))
    assert at_decision.regime is later.regime is Regime.TREND
    assert later.efficiency == at_decision.efficiency


def test_bars_not_yet_complete_are_ignored(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    # A bar that opens exactly at the decision time is not complete at it.
    bars.append(bar(_decision(instrument), 130.0, 130.0, 1.0, 1.0))
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.TREND


def test_outside_the_prime_session_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    saturday = datetime(2026, 10, 17, 17, 0, tzinfo=UTC)
    call = _reader(instrument, data).call(bars, saturday)
    assert call.regime is Regime.STAND_ASIDE
    assert call.day is None


def test_as_of_must_be_utc(instrument: InstrumentProfile, data: DataSettings) -> None:
    with pytest.raises(ValueError, match="as_of"):
        _reader(instrument, data).call([], datetime(2026, 10, 14, 7, 0))  # noqa: DTZ001


# --- missing or unusable data fails closed ---------------------------------------


def test_too_little_history_stands_aside(instrument: InstrumentProfile, data: DataSettings) -> None:
    bars = _day_bars(TODAY, instrument, closes=_closes_for("clean"), night_range=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "history" in call.reason


def test_a_gap_in_todays_opening_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    open_ = _cash_open(TODAY, instrument)
    gap = {open_ + MINUTE * i for i in range(10, 10 + data.max_gap_minutes + 1)}
    bars = [b for b in bars if b.ts not in gap]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "opening" in call.reason


def test_the_last_opening_minute_is_required(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    last = _cash_open(TODAY, instrument) + MINUTE * (SMALL.decision_minutes - 1)
    bars = [b for b in bars if b.ts != last]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE


def test_a_missing_overnight_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    open_ = _cash_open(TODAY, instrument)
    night = _night_open(TODAY, instrument)
    bars = [b for b in bars if not night <= b.ts < open_]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "overnight" in call.reason


def test_a_bad_history_day_stands_aside_and_is_named(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    bad = _history(SMALL.efficiency_lookback_sessions, instrument)[2]
    open_ = _cash_open(bad, instrument)
    bars = [b for b in bars if not open_ <= b.ts < open_ + MINUTE * 30]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert bad.isoformat() in call.reason


def test_a_flat_opening_has_no_efficiency(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("flat"), today_night=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert call.efficiency is None


def test_history_skips_non_trading_days(instrument: InstrumentProfile) -> None:
    # The history helper (and the reader) walk trading days only: no weekend inside.
    for day in _history(SMALL.efficiency_lookback_sessions, instrument):
        assert is_trading_day(day, instrument)


def test_the_call_is_a_value_object(instrument: InstrumentProfile, data: DataSettings) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert isinstance(call, RegimeCall)
    assert call.ts == _decision(instrument)
    assert _reader(instrument, data).agent_id == "regime"


def test_efficiency_rank_uses_only_its_own_lookback(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Overnight lookback (6) longer than the efficiency lookback (4): the 2 oldest days
    # (the lowest efficiencies) must not enter the rank.
    s = replace(SMALL, efficiency_lookback_sessions=4, overnight_lookback_sessions=6)
    history = [_mid_closes(k) for k in range(6)]  # efficiencies rise with k
    bars = _build(instrument, today_closes=_mid_closes(2), today_night=20.0, history_closes=history)
    call = _reader(instrument, data, s).call(bars, _decision(instrument))
    # Of the last 4 days (k = 2..5), none is strictly below today's (k = 2).
    assert call.efficiency_rank_pct == Decimal(0)


def test_a_flat_history_opening_stands_aside_and_is_named(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    history = [_mid_closes(k) for k in range(6)]
    history[3] = _closes_for("flat")
    bars = _build(
        instrument, today_closes=_closes_for("clean"), today_night=20.0, history_closes=history
    )
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    day = _history(SMALL.efficiency_lookback_sessions, instrument)[3]
    assert day.isoformat() in call.reason
    assert "did not move" in call.reason


def test_a_thin_opening_without_long_gaps_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Every other minute missing: no gap is long, but half the minutes are gone.
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    open_ = _cash_open(TODAY, instrument)
    drop = {open_ + MINUTE * i for i in range(1, SMALL.decision_minutes - 1, 2)}
    bars = [b for b in bars if b.ts not in drop]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "opening" in call.reason
    assert "minutes" in call.reason


def test_one_long_gap_overnight_stands_aside_despite_good_coverage(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    middle = _night_open(TODAY, instrument) + timedelta(hours=5)
    gap = {middle + MINUTE * i for i in range(data.max_gap_minutes + 1)}
    bars = [b for b in bars if b.ts not in gap]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "consecutive missing minutes" in call.reason


def test_a_late_start_overnight_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    night = _night_open(TODAY, instrument)
    late = {night + MINUTE * i for i in range(data.max_gap_minutes + 1)}
    bars = [b for b in bars if b.ts not in late]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "overnight" in call.reason


# --- review round 1 (regime) -----------------------------------------------------


def test_a_clean_falling_opening_is_a_trend_too(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    falling = [100.0 - (i + 1) for i in range(30)]
    bars = _build(instrument, today_closes=falling, today_night=20.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.efficiency == Decimal(1)
    assert call.regime is Regime.TREND


def test_the_first_move_counts_in_the_path() -> None:
    # One bar from 100 to 101, then back to 100.5: path 1 + 0.5, net 0.5.
    b0 = bar(datetime(2026, 10, 14, 13, 30, tzinfo=UTC), 100.0, 101.0, 100.0, 101.0)
    b1 = bar(datetime(2026, 10, 14, 13, 31, tzinfo=UTC), 101.0, 101.0, 100.5, 100.5)
    assert opening_efficiency([b0, b1]) == Decimal("0.5") / Decimal("1.5")
    assert opening_efficiency([b0]) == Decimal(1)


def test_opening_efficiency_needs_a_bar() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        opening_efficiency([])


def test_classify_needs_history() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        classify(Decimal(1), [], Decimal(1), [Decimal(1)], SMALL)
    with pytest.raises(ValueError, match="non-empty"):
        classify(Decimal(1), [Decimal(1)], Decimal(1), [], SMALL)


def test_classify_is_the_rule_the_reader_uses() -> None:
    past = [Decimal(k) / 10 for k in range(6)]  # 0.0 .. 0.5
    c = classify(Decimal("0.45"), past, Decimal(12), [Decimal(10), Decimal(14)], SMALL)
    # 5 of 6 strictly lower -> 83.3: trend rank; night 12 >= median 12 -> TREND.
    assert c.regime is Regime.TREND
    assert c.rank_pct == Decimal(100) * 5 / 6
    assert c.overnight_median == Decimal(12)


def test_a_gap_of_exactly_the_limit_is_usable(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    middle = _night_open(TODAY, instrument) + timedelta(hours=5)
    gap = {middle + MINUTE * i for i in range(data.max_gap_minutes)}
    bars = [b for b in bars if b.ts not in gap]
    assert _reader(instrument, data).call(bars, _decision(instrument)).regime is Regime.TREND


def test_coverage_exactly_at_the_minimum_is_usable(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # 30-minute opening, 90% coverage: 27 bars needed. Drop 3 single minutes inside.
    assert data.min_session_coverage_pct == 90
    open_ = _cash_open(TODAY, instrument)
    for dropped, regime in ((3, Regime.TREND), (4, Regime.STAND_ASIDE)):
        bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
        drop = {open_ + MINUTE * (2 + 3 * i) for i in range(dropped)}
        bars = [b for b in bars if b.ts not in drop]
        call = _reader(instrument, data).call(bars, _decision(instrument))
        assert call.regime is regime, (dropped, call.reason)


def test_a_stall_right_before_the_cash_open_makes_the_overnight_unusable(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    open_ = _cash_open(TODAY, instrument)
    stall = {open_ - MINUTE * (i + 1) for i in range(data.max_gap_minutes + 1)}
    bars = [b for b in bars if b.ts not in stall]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "today's overnight" in call.reason


def test_the_overnight_does_not_need_its_last_minute(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    last = _cash_open(TODAY, instrument) - MINUTE
    bars = [b for b in bars if b.ts != last]
    assert _reader(instrument, data).call(bars, _decision(instrument)).regime is Regime.TREND


def test_the_overnight_range_includes_its_first_and_last_minutes(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    first = _night_open(TODAY, instrument)
    last = _cash_open(TODAY, instrument) - MINUTE
    bars = [
        bar(b.ts, 100.0, 140.0, 90.0, 100.0)
        if b.ts == first
        else bar(b.ts, 100.0, 110.0, 70.0, 100.0)
        if b.ts == last
        else b
        for b in bars
    ]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.overnight_range == Decimal(70)  # 140 (first minute) - 70 (last minute)


def _shifted_night(inst: InstrumentProfile, night: Session) -> InstrumentProfile:
    return replace(inst, overnight_session=night)


def test_the_overnight_window_uses_its_own_time_zone(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # 17:00 Chicago is 15:00 Pacific: the same instant as the config's overnight open.
    chicago = _shifted_night(
        instrument, Session(start=time(17, 0), end=time(5, 59), timezone="America/Chicago")
    )
    bars = _build(chicago, today_closes=_closes_for("clean"), today_night=20.0)
    first = _night_open(TODAY, chicago)
    assert first == _night_open(TODAY, instrument)
    bars = [bar(b.ts, 100.0, 140.0, 90.0, 100.0) if b.ts == first else b for b in bars]
    call = _reader(chicago, data).call(bars, _decision(chicago))
    # Read in the cash session's zone, 17:00 would start two hours later and miss it.
    assert call.overnight_range == Decimal(50)


def test_a_same_day_overnight_starts_on_the_day_itself(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    early = _shifted_night(
        instrument, Session(start=time(0, 0), end=time(3, 59), timezone="America/Los_Angeles")
    )
    bars: list[Bar] = []
    for day in [*_history(SMALL.efficiency_lookback_sessions, early), TODAY]:
        closes = _closes_for("clean") if day == TODAY else _mid_closes(day.toordinal() % 6)
        begin = datetime.combine(day, time(0, 0), PT).astimezone(UTC)
        bars += _day_bars(day, early, closes=closes, night_range=20.0, night_begin=begin)
    call = _reader(early, data).call(bars, _decision(early))
    # Starting the night the day before would find 15 hours of missing minutes.
    assert call.regime is Regime.TREND, call.reason
    assert call.overnight_range == Decimal(20)


def test_a_trend_rank_of_100_is_a_valid_setting(tmp_path: Path) -> None:
    assert load_regime(_write(tmp_path, trend_rank_pct=100)).trend_rank_pct == 100


def test_decision_minutes_must_end_inside_the_cash_session(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    cash = instrument.cash_session
    length = (
        datetime.combine(date.min, cash.end) - datetime.combine(date.min, cash.start)
    ) // MINUTE
    with pytest.raises(ValueError, match="inside the cash session"):
        RegimeReader(replace(SMALL, decision_minutes=length), data, instrument)
    RegimeReader(replace(SMALL, decision_minutes=length - 1), data, instrument)


def test_history_sessions_is_today_plus_the_longer_lookback(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    assert _reader(instrument, data).history_sessions == 7
    other = replace(SMALL, efficiency_lookback_sessions=3, overnight_lookback_sessions=5)
    assert _reader(instrument, data, other).history_sessions == 6


# --- bars that cannot be trusted ----------------------------------------------------


def test_unordered_bars_stand_aside(instrument: InstrumentProfile, data: DataSettings) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    bars[10], bars[11] = bars[11], bars[10]
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "out of order or duplicated" in call.reason


def test_duplicated_bars_stand_aside(instrument: InstrumentProfile, data: DataSettings) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    bars.insert(-5, bars[-5])
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "out of order or duplicated" in call.reason


def test_a_bar_off_the_whole_minute_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    b = bars[-10]
    bars[-10] = bar(b.ts + timedelta(seconds=30), 100.0, 200.0, 99.0, 150.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "whole minute" in call.reason


# --- the label is kept for the day; data failures are not ---------------------------


def test_a_label_is_kept_when_the_oldest_history_slides_out(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    reader = _reader(instrument, data)
    first = reader.call(bars, _decision(instrument))
    oldest = _history(SMALL.efficiency_lookback_sessions, instrument)[0]
    later_window = [b for b in bars if b.ts >= _cash_open(oldest, instrument) + timedelta(days=1)]
    later = reader.call(later_window, _decision(instrument) + timedelta(hours=2))
    assert first.regime is later.regime is Regime.TREND
    assert later.ts == _decision(instrument) + timedelta(hours=2)
    # A fresh reader with the same short window cannot label the day.
    fresh = _reader(instrument, data).call(later_window, _decision(instrument) + timedelta(hours=2))
    assert fresh.regime is Regime.STAND_ASIDE


def test_a_data_failure_is_not_kept(instrument: InstrumentProfile, data: DataSettings) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    reader = _reader(instrument, data)
    last = _cash_open(TODAY, instrument) + MINUTE * (SMALL.decision_minutes - 1)
    missing = reader.call([b for b in bars if b.ts != last], _decision(instrument))
    assert missing.regime is Regime.STAND_ASIDE
    assert reader.call(bars, _decision(instrument) + MINUTE).regime is Regime.TREND


def test_a_kept_label_does_not_carry_to_the_next_day(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    reader = _reader(instrument, data)
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    assert reader.call(bars, _decision(instrument)).regime is Regime.TREND
    tomorrow = date(2026, 10, 15)
    chop = _build(instrument, today_closes=_closes_for("chop"), today_night=20.0, today=tomorrow)
    assert reader.call(chop, _decision(instrument, tomorrow)).regime is Regime.CHOP


# --- calendar shapes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "today",
    [
        date(2026, 10, 19),  # a Monday: the overnight opened Sunday evening
        date(2026, 11, 4),  # history crosses the end of daylight time (2026-11-01)
        date(2026, 11, 30),  # the Monday after Thanksgiving week's closures
    ],
)
def test_calendar_shapes(instrument: InstrumentProfile, data: DataSettings, today: date) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0, today=today)
    call = _reader(instrument, data).call(bars, _decision(instrument, today))
    assert call.regime is Regime.TREND, call.reason
    assert call.overnight_median == Decimal(10)


def test_history_before_the_maintained_calendar_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Holidays before calendar_from are unknown, so no history day may come from there.
    short = replace(instrument, calendar_from=TODAY - timedelta(days=3))
    bars = _build(short, today_closes=_closes_for("clean"), today_night=20.0)
    call = _reader(short, data).call(bars, _decision(short))
    assert call.regime is Regime.STAND_ASIDE
    assert "before the maintained calendar" in call.reason


def test_no_trading_day_in_the_search_window_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Every one of the previous 40 days a holiday: the calendar's search gives up.
    closed = frozenset(TODAY - timedelta(days=d) for d in range(1, 40))
    blocked = replace(instrument, holidays=instrument.holidays | closed)
    bars = _day_bars(TODAY, blocked, closes=_closes_for("clean"), night_range=20.0)
    call = _reader(blocked, data).call(bars, _decision(blocked))
    assert call.regime is Regime.STAND_ASIDE
    assert call.reason.startswith("history: no trading day")


def test_the_overnight_session_is_not_a_place_to_label(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    evening = datetime.combine(TODAY, time(18, 0), PT).astimezone(UTC)  # overnight session
    call = _reader(instrument, data).call(bars, evening)
    assert call.regime is Regime.STAND_ASIDE
    assert call.day is None
    assert call.reason == "outside the prime session"


# --- review round 2 (C1, C2) --------------------------------------------------------


def test_a_kept_label_is_not_served_for_different_bars(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    reader = _reader(instrument, data)
    chop = _build(instrument, today_closes=_closes_for("chop"), today_night=20.0)
    clean = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    assert reader.call(chop, _decision(instrument)).regime is Regime.CHOP
    # Same reader, same day, a different data set: a new label, not the kept CHOP.
    again = reader.call(clean, _decision(instrument) + MINUTE)
    assert again.regime is Regime.TREND
    assert again.efficiency == Decimal(1)


def test_a_rule_based_stand_aside_is_kept_too(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    # Clean opening after a narrow night: STAND_ASIDE by the rule, from sound data.
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=5.0)
    reader = _reader(instrument, data)
    first = reader.call(bars, _decision(instrument))
    assert first.regime is Regime.STAND_ASIDE
    assert first.efficiency_rank_pct is not None  # made by the rule, not a data failure
    oldest = _history(SMALL.efficiency_lookback_sessions, instrument)[0]
    slid = [b for b in bars if b.ts >= _cash_open(oldest, instrument) + timedelta(days=1)]
    later = reader.call(slid, _decision(instrument) + timedelta(hours=2))
    assert later.reason == first.reason
    assert later.efficiency_rank_pct == first.efficiency_rank_pct


def test_the_first_day_of_the_calendar_may_be_history(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    oldest = _history(SMALL.efficiency_lookback_sessions, instrument)[0]
    starts_there = replace(instrument, calendar_from=oldest)
    bars = _build(starts_there, today_closes=_closes_for("clean"), today_night=20.0)
    call = _reader(starts_there, data).call(bars, _decision(starts_there))
    assert call.regime is Regime.TREND, call.reason
    one_later = replace(instrument, calendar_from=oldest + timedelta(days=1))
    call = _reader(one_later, data).call(bars, _decision(one_later))
    assert call.regime is Regime.STAND_ASIDE
    assert "before the maintained calendar" in call.reason


def test_a_bar_a_microsecond_off_the_minute_stands_aside(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    b = bars[-10]
    bars[-10] = bar(b.ts + timedelta(microseconds=1), 100.0, 200.0, 99.0, 150.0)
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.regime is Regime.STAND_ASIDE
    assert "whole minute" in call.reason


def test_a_corrected_overnight_for_today_makes_a_new_label(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    reader = _reader(instrument, data)
    wide = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    narrow = _build(instrument, today_closes=_closes_for("clean"), today_night=5.0)
    assert reader.call(wide, _decision(instrument)).regime is Regime.TREND
    # Same opening bars, a different overnight: the kept TREND must not be served.
    assert reader.call(narrow, _decision(instrument) + MINUTE).regime is Regime.STAND_ASIDE


def test_a_history_data_failure_is_not_kept(
    instrument: InstrumentProfile, data: DataSettings
) -> None:
    bars = _build(instrument, today_closes=_closes_for("clean"), today_night=20.0)
    bad = _history(SMALL.efficiency_lookback_sessions, instrument)[2]
    open_ = _cash_open(bad, instrument)
    holed = [b for b in bars if not open_ <= b.ts < open_ + MINUTE * 30]
    reader = _reader(instrument, data)
    assert reader.call(holed, _decision(instrument)).regime is Regime.STAND_ASIDE
    # Today's bars are the same; the history arrived: the failure was not kept.
    assert reader.call(bars, _decision(instrument) + MINUTE).regime is Regime.TREND
