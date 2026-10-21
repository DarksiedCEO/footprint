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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from footprint.agents.regime_reader import Regime, RegimeCall, RegimeReader
from footprint.core.calendar import is_trading_day, previous_trading_day
from footprint.core.config import (
    ConfigError,
    DataSettings,
    InstrumentProfile,
    RegimeSettings,
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
    return datetime.combine(day, inst.cash_session.start, PT).astimezone(UTC)


def _night_open(day: date, inst: InstrumentProfile) -> datetime:
    night = inst.overnight_session
    start_day = day - timedelta(days=1) if night.start > night.end else day
    return datetime.combine(start_day, night.start, PT).astimezone(UTC)


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
) -> list[Bar]:
    out: list[Bar] = []
    ts = _night_open(day, inst)
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


def _history(n: int, inst: InstrumentProfile) -> list[date]:
    days: list[date] = []
    day = TODAY
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
) -> list[Bar]:
    n = SMALL.efficiency_lookback_sessions
    days = _history(n, inst)
    h_closes = history_closes or [_mid_closes(k) for k in range(n)]
    h_nights = history_nights or [10.0] * n
    bars: list[Bar] = []
    for day, closes, night in zip(days, h_closes, h_nights, strict=True):
        bars += _day_bars(day, inst, closes=closes, night_range=night)
    bars += _day_bars(TODAY, inst, closes=today_closes, night_range=today_night)
    return bars


def _mid_closes(k: int) -> list[float]:
    """History day k: net 10 + k points up over a path of 30 + k... efficiencies spread
    strictly between chop and clean, all different, so ranks are unambiguous."""
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
    # The 3 oldest history nights are wide, but only the last 4 count: [150, 10, 10, 10],
    # median 10 (the mean would be 45, the median of all six 80).
    nights = [150.0, 150.0, 150.0, 10.0, 10.0, 10.0]
    bars = _build(
        instrument, today_closes=_closes_for("clean"), today_night=10.0, history_nights=nights
    )
    call = _reader(instrument, data).call(bars, _decision(instrument))
    assert call.overnight_median == Decimal(10)
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
