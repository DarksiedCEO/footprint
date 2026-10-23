"""The play caller (decision/play.py): the regime reader's label, wired to the entries.

Written before the code. Andre's decisions:
- PC-5 (2026-10-22): a TREND day runs the existing breakout entry; a CHOP day and a
  STAND ASIDE day take no new entries.
- PC-4: only a label made on time counts. The day's label is fixed by the FIRST
  decision at or after the decision time: inside `play.on_time_seconds` of it, the
  reader's label stands for the whole day (a data failure then included: no retry);
  later, the day stands aside (LATE) and the reader is not asked. A bad overnight
  stands aside (the reader's own rule).
- No new entries before the decision time, on any day (the reader is not asked).
- PC-3: a day whose history would reach before calendar_from is WARM_UP (stand aside).
- PC-2: the reader needs `history_sessions` of bars; live, market_data.history_days
  must cover them on every day of the maintained calendar, or the start is refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from footprint.agents.regime_reader import Regime, RegimeReader
from footprint.core.calendar import previous_trading_day
from footprint.core.config import (
    ConfigError,
    InstrumentProfile,
    PlaySettings,
    Profiles,
    load_play,
    load_profiles,
)
from footprint.core.models import Bar
from footprint.decision.play import (
    PlayCall,
    PlayCaller,
    PlayCause,
    PlayHistoryTooShort,
    check_live_history,
    history_days_needed,
)
from tests.agents.test_regime_reader import (
    SMALL,
    TODAY,
    _build,
    _cash_open,
    _closes_for,
    _decision,
    _night_open,
)
from tests.conftest import REPO_ROOT

MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)
PLAY = PlaySettings(on_time_seconds=60)


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


@pytest.fixture(scope="module")
def inst(profiles: Profiles) -> InstrumentProfile:
    return profiles.instrument


def caller(
    profiles: Profiles,
    history: object = None,
    play: PlaySettings = PLAY,
) -> PlayCaller:
    return PlayCaller(
        SMALL,
        play,
        profiles.strategy.data,
        profiles.instrument,
        history=history,  # type: ignore[arg-type]
    )


def trend_day(inst: InstrumentProfile, today: date = TODAY) -> list[Bar]:
    return _build(inst, today_closes=_closes_for("clean"), today_night=20.0, today=today)


def chop_day(inst: InstrumentProfile) -> list[Bar]:
    return _build(inst, today_closes=_closes_for("chop"), today_night=20.0)


def narrow_night_day(inst: InstrumentProfile) -> list[Bar]:
    """A clean opening after a night narrower than its median: the rules stand aside."""
    return _build(inst, today_closes=_closes_for("clean"), today_night=5.0)


class Counting:
    """A history source that counts its calls and hands out `bars`."""

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.bars = tuple(bars)
        self.calls: list[datetime] = []

    def __call__(self, now: datetime) -> Sequence[Bar]:
        self.calls.append(now)
        return self.bars


# --- config ----------------------------------------------------------------------


def test_repo_play_config_loads(profiles: Profiles) -> None:
    assert profiles.play == load_play(REPO_ROOT / "config" / "play.yaml")
    assert profiles.play.on_time_seconds >= 1
    assert profiles.regime.decision_minutes >= 1


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "true", "'60'"])
def test_play_config_refuses_a_bad_on_time_window(tmp_path: object, bad: str) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "play.yaml"
    path.write_text(f"on_time_seconds: {bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="on_time_seconds"):
        load_play(path)


def test_play_config_refuses_unknown_and_missing_keys(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "play.yaml"
    path.write_text("on_time_seconds: 60\nextra: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        load_play(path)
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing"):
        load_play(path)


# --- PC-5: which play runs on which label ---------------------------------------


def test_a_trend_day_allows_entries_from_the_decision_time(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    call = caller(profiles).call(trend_day(inst), _decision(inst))
    assert call.regime is Regime.TREND
    assert call.cause is PlayCause.TREND
    assert call.entries_allowed
    assert call.first
    assert call.day == TODAY
    assert call.label is not None
    assert call.label.regime is Regime.TREND
    assert "TREND" in call.reason


def test_a_chop_day_takes_no_new_entries(profiles: Profiles, inst: InstrumentProfile) -> None:
    call = caller(profiles).call(chop_day(inst), _decision(inst))
    assert call.regime is Regime.CHOP
    assert call.cause is PlayCause.CHOP
    assert not call.entries_allowed
    assert "CHOP" in call.reason


def test_a_day_the_rules_call_neither_stands_aside(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    call = caller(profiles).call(narrow_night_day(inst), _decision(inst))
    assert call.regime is Regime.STAND_ASIDE
    assert call.cause is PlayCause.RULES
    assert not call.entries_allowed
    assert call.label is not None
    assert call.label.reason in call.reason


def _regime_for(cause: PlayCause) -> Regime:
    return {PlayCause.TREND: Regime.TREND, PlayCause.CHOP: Regime.CHOP}.get(
        cause, Regime.STAND_ASIDE
    )


def test_only_trend_allows_entries() -> None:
    allowed = {
        cause
        for cause in PlayCause
        if PlayCall(None, _regime_for(cause), cause, "").entries_allowed
    }
    assert allowed == {PlayCause.TREND}


@pytest.mark.parametrize(
    ("regime", "cause", "match"),
    [
        (Regime.CHOP, PlayCause.TREND, "TREND"),
        (Regime.STAND_ASIDE, PlayCause.TREND, "TREND"),
        (Regime.TREND, PlayCause.CHOP, "TREND"),
        (Regime.TREND, PlayCause.RULES, "TREND"),
        (Regime.STAND_ASIDE, PlayCause.CHOP, "CHOP"),
        (Regime.CHOP, PlayCause.RULES, "CHOP"),
        (Regime.CHOP, PlayCause.NO_DATA, "CHOP"),
    ],
)
def test_a_play_must_match_its_label(regime: Regime, cause: PlayCause, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        PlayCall(TODAY, regime, cause, "mismatch")


# --- the label is fixed once a day --------------------------------------------------


def test_the_label_is_kept_for_the_day_and_journaled_once(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    source = Counting(trend_day(inst))
    play = caller(profiles, history=source)
    first = play.call((), _decision(inst))
    later = [play.call((), _decision(inst) + MINUTE * k) for k in (1, 2, 90)]
    assert first.first
    assert first.entries_allowed
    assert all(not c.first for c in later)
    assert all(c.entries_allowed and c.cause is PlayCause.TREND for c in later)
    assert all(c.label == first.label for c in later)
    assert len(source.calls) == 1  # the reader is asked once a day


def test_later_data_never_changes_the_days_label(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    play = caller(profiles)
    assert play.call(chop_day(inst), _decision(inst)).cause is PlayCause.CHOP
    # The same day, the trend data: still CHOP (fixed at the decision minute).
    assert play.call(trend_day(inst), _decision(inst) + MINUTE).cause is PlayCause.CHOP


def test_a_new_day_gets_a_new_label(profiles: Profiles, inst: InstrumentProfile) -> None:
    play = caller(profiles)
    assert play.call(chop_day(inst), _decision(inst)).cause is PlayCause.CHOP
    tomorrow = TODAY + timedelta(days=1)
    call = play.call(trend_day(inst, tomorrow), _decision(inst, tomorrow))
    assert call.first
    assert call.day == tomorrow
    assert call.cause is PlayCause.TREND


# --- no new entries before the decision time ----------------------------------------


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(minutes=-SMALL.decision_minutes),  # the cash open
        timedelta(minutes=-1),
        -SECOND,
    ],
)
def test_no_entries_in_the_cash_session_before_the_decision_time(
    profiles: Profiles, inst: InstrumentProfile, offset: timedelta
) -> None:
    source = Counting(trend_day(inst))
    play = caller(profiles, history=source)
    call = play.call((), _decision(inst) + offset)
    assert call.cause is PlayCause.BEFORE_DECISION
    assert not call.entries_allowed
    assert not call.first
    assert call.regime is Regime.STAND_ASIDE
    assert source.calls == []  # not asked, and nothing fixed: the decision minute still counts
    assert play.call((), _decision(inst)).cause is PlayCause.TREND


def test_no_entries_in_prime_before_the_cash_open(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    zone = inst.prime_session.timezone
    from zoneinfo import ZoneInfo

    prime_open = datetime.combine(TODAY, inst.prime_session.start, ZoneInfo(zone)).astimezone(UTC)
    call = caller(profiles).call(trend_day(inst), prime_open)
    assert call.cause is PlayCause.BEFORE_DECISION
    assert call.day == TODAY
    assert not call.entries_allowed


def test_outside_prime_no_entries(profiles: Profiles, inst: InstrumentProfile) -> None:
    night = _night_open(TODAY, inst) + MINUTE * 5
    source = Counting(trend_day(inst))
    call = caller(profiles, history=source).call((), night)
    assert call.cause is PlayCause.OUTSIDE_PRIME
    assert call.day is None
    assert not call.entries_allowed
    assert source.calls == []


def test_the_decision_time_is_the_readers(profiles: Profiles, inst: InstrumentProfile) -> None:
    """The play caller and the reader agree on the decision time: one second before it
    the reader too says 'before the decision time'; at it the reader labels."""
    reader = RegimeReader(SMALL, profiles.strategy.data, inst)
    bars = trend_day(inst)
    assert reader.call(bars, _decision(inst) - SECOND).reason.startswith("before the decision")
    assert reader.call(bars, _decision(inst)).regime is Regime.TREND


# --- PC-4: only a label made on time counts ---------------------------------------


def test_the_last_second_of_the_window_is_on_time(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    edge = _decision(inst) + timedelta(seconds=PLAY.on_time_seconds) - SECOND
    assert caller(profiles).call(trend_day(inst), edge).cause is PlayCause.TREND


@pytest.mark.parametrize("late", [timedelta(0), SECOND, timedelta(hours=2)])
def test_a_first_decision_after_the_window_is_late_and_stands_aside_all_day(
    profiles: Profiles, inst: InstrumentProfile, late: timedelta
) -> None:
    source = Counting(trend_day(inst))
    play = caller(profiles, history=source)
    at = _decision(inst) + timedelta(seconds=PLAY.on_time_seconds) + late
    call = play.call((), at)
    assert call.cause is PlayCause.LATE
    assert call.regime is Regime.STAND_ASIDE
    assert not call.entries_allowed
    assert call.first  # journaled: the day's label is "late"
    assert call.label is None
    assert source.calls == []  # the reader is not asked
    again = play.call((), at + MINUTE)
    assert again.cause is PlayCause.LATE
    assert not again.first


def test_the_window_comes_from_config(profiles: Profiles, inst: InstrumentProfile) -> None:
    wide = PlaySettings(on_time_seconds=PLAY.on_time_seconds * 3)
    at = _decision(inst) + timedelta(seconds=PLAY.on_time_seconds * 2)
    assert caller(profiles, play=wide).call(trend_day(inst), at).cause is PlayCause.TREND
    assert caller(profiles).call(trend_day(inst), at).cause is PlayCause.LATE


def test_data_unusable_at_the_decision_minute_stands_aside_all_day(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    """The final opening minute missing on time: NO_DATA, fixed for the day, even when
    the minute arrives a moment later (a late label never counts)."""
    full = trend_day(inst)
    last_opening = _decision(inst) - MINUTE
    missing = [b for b in full if b.ts != last_opening]
    play = caller(profiles)
    call = play.call(missing, _decision(inst))
    assert call.cause is PlayCause.NO_DATA
    assert call.regime is Regime.STAND_ASIDE
    assert call.first
    assert call.label is not None
    assert "final minute" in call.label.reason
    assert play.call(full, _decision(inst) + SECOND).cause is PlayCause.NO_DATA


def test_a_bad_overnight_stands_aside(profiles: Profiles, inst: InstrumentProfile) -> None:
    bars = [
        b
        for b in chop_day(inst)
        if not (_night_open(TODAY, inst) <= b.ts < _cash_open(TODAY, inst))
    ]
    call = caller(profiles).call(bars, _decision(inst))
    assert call.cause is PlayCause.NO_DATA
    assert not call.entries_allowed
    assert call.label is not None
    assert "overnight" in call.label.reason


def test_a_reader_failure_stands_aside_all_day(profiles: Profiles, inst: InstrumentProfile) -> None:
    calls: list[datetime] = []

    def broken(now: datetime) -> Sequence[Bar]:
        calls.append(now)
        raise ValueError("feed exploded")

    play = caller(profiles, history=broken)
    call = play.call((), _decision(inst))
    assert call.cause is PlayCause.READER_FAILED
    assert "feed exploded" in call.reason
    assert call.first
    assert not call.entries_allowed
    assert play.call((), _decision(inst) + MINUTE).cause is PlayCause.READER_FAILED
    assert len(calls) == 1


def test_a_type_error_in_the_reader_also_stands_aside(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    def broken(now: datetime) -> Sequence[Bar]:
        raise TypeError("not bars")

    assert caller(profiles, history=broken).call((), _decision(inst)).cause is (
        PlayCause.READER_FAILED
    )


def test_other_exceptions_are_not_swallowed(profiles: Profiles, inst: InstrumentProfile) -> None:
    def broken(now: datetime) -> Sequence[Bar]:
        raise RuntimeError("a defect")

    with pytest.raises(RuntimeError):
        caller(profiles, history=broken).call((), _decision(inst))


# --- the history source -------------------------------------------------------------


def test_without_its_own_source_it_reads_the_bars_handed_to_it(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    assert caller(profiles).call(trend_day(inst), _decision(inst)).cause is PlayCause.TREND
    assert caller(profiles).call(chop_day(inst), _decision(inst)).cause is PlayCause.CHOP


def test_its_own_source_wins_over_the_bars_handed_to_it(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    source = Counting(trend_day(inst))
    call = caller(profiles, history=source).call(chop_day(inst), _decision(inst))
    assert call.cause is PlayCause.TREND
    assert source.calls == [_decision(inst)]


def test_too_little_history_stands_aside(profiles: Profiles, inst: InstrumentProfile) -> None:
    bars = trend_day(inst)
    oldest = previous_trading_day(previous_trading_day(TODAY, inst), inst)
    short = [b for b in bars if b.ts >= _night_open(oldest, inst) + timedelta(days=1)]
    call = caller(profiles).call(short, _decision(inst))
    assert call.cause is PlayCause.NO_DATA


def test_history_sessions_is_the_readers(profiles: Profiles) -> None:
    reader = RegimeReader(SMALL, profiles.strategy.data, profiles.instrument)
    assert caller(profiles).history_sessions == reader.history_sessions
    real = PlayCaller(
        profiles.regime, profiles.play, profiles.strategy.data, profiles.instrument
    ).history_sessions
    assert (
        real
        == max(
            profiles.regime.efficiency_lookback_sessions,
            profiles.regime.overnight_lookback_sessions,
        )
        + 1
    )


def test_as_of_must_be_utc(profiles: Profiles, inst: InstrumentProfile) -> None:
    with pytest.raises(ValueError, match="as_of"):
        caller(profiles).call(trend_day(inst), _decision(inst).replace(tzinfo=None))


# --- PC-3: the calendar warm-up -------------------------------------------------------


def _first_labellable(inst: InstrumentProfile, sessions: int) -> date:
    """The first trading day whose `sessions - 1` previous trading days all lie inside
    the maintained calendar (walked here independently of the code)."""
    day = inst.calendar_from
    while True:
        day += timedelta(days=1)
        if day.weekday() >= 5 or day in inst.holidays or day in inst.no_trade_days:
            continue
        back, ok = day, True
        for _ in range(sessions - 1):
            back = previous_trading_day(back, inst)
            if back < inst.calendar_from:
                ok = False
                break
        if ok:
            return day


def test_a_day_whose_history_reaches_before_the_calendar_is_warm_up(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    first = _first_labellable(inst, caller(profiles).history_sessions)
    warm = previous_trading_day(first, inst)
    bars = trend_day(inst, warm)
    source = Counting(bars)
    call = caller(profiles, history=source).call((), _decision(inst, warm))
    assert call.cause is PlayCause.WARM_UP
    assert call.first
    assert not call.entries_allowed
    assert source.calls == []
    assert inst.calendar_from.isoformat() in call.reason
    # The reader agrees: on the same bars it refuses history before the calendar.
    reader = RegimeReader(SMALL, profiles.strategy.data, inst)
    assert "before the maintained calendar" in reader.call(bars, _decision(inst, warm)).reason


def test_the_first_day_after_the_warm_up_is_labelled(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    first = _first_labellable(inst, caller(profiles).history_sessions)
    call = caller(profiles).call(trend_day(inst, first), _decision(inst, first))
    assert call.cause is PlayCause.TREND


# --- PC-2: the live history must cover the reader -------------------------------------


def test_the_repo_live_history_covers_the_reader(profiles: Profiles) -> None:
    check_live_history(profiles)  # no exception
    assert profiles.execution.market_data.history_days >= history_days_needed(profiles)


def _with_history_days(profiles: Profiles, days: int) -> Profiles:
    market = replace(profiles.execution.market_data, history_days=days)
    return replace(profiles, execution=replace(profiles.execution, market_data=market))


def test_one_day_too_few_is_refused_with_a_clear_message(profiles: Profiles) -> None:
    need = history_days_needed(profiles)
    check_live_history(_with_history_days(profiles, need))
    with pytest.raises(PlayHistoryTooShort) as refused:
        check_live_history(_with_history_days(profiles, need - 1))
    message = str(refused.value)
    assert "history_days" in message
    assert str(need) in message
    assert str(need - 1) in message
    assert isinstance(refused.value, ValueError)


def test_the_need_grows_with_the_lookback(profiles: Profiles) -> None:
    longer = replace(profiles.regime, efficiency_lookback_sessions=100)
    assert history_days_needed(replace(profiles, regime=longer)) > history_days_needed(profiles)
    shorter = replace(
        profiles.regime, efficiency_lookback_sessions=10, overnight_lookback_sessions=10
    )
    assert history_days_needed(replace(profiles, regime=shorter)) < history_days_needed(profiles)


def test_the_overnight_lookback_counts_too(profiles: Profiles) -> None:
    longer = replace(profiles.regime, overnight_lookback_sessions=100)
    assert history_days_needed(replace(profiles, regime=longer)) > history_days_needed(profiles)


def test_a_lookback_no_calendar_day_can_meet_is_refused(profiles: Profiles) -> None:
    huge = replace(profiles.regime, efficiency_lookback_sessions=5000)
    with pytest.raises(PlayHistoryTooShort, match="calendar"):
        history_days_needed(replace(profiles, regime=huge))


def _utc_day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def test_the_need_is_exactly_what_the_reader_needs_on_the_worst_day(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    """Live keeps bars from 00:00 UTC `history_days` days before the UTC date of the
    decision (market data's history window). With exactly the computed need, the reader
    labels the worst day of the calendar; with one day less it cannot."""
    small = replace(profiles, regime=SMALL)
    need = history_days_needed(small)
    sessions = caller(profiles).history_sessions
    worst = None
    day = inst.calendar_from
    while day <= inst.calendar_through:
        if day.weekday() < 5 and day not in inst.holidays and day not in inst.no_trade_days:
            # What the reader reads: today's night and opening, the openings of the
            # previous efficiency_lookback_sessions days, the nights of the previous
            # overnight_lookback_sessions days.
            back, ok, oldest = day, True, _night_open(day, inst)
            for count in range(sessions - 1):
                back = previous_trading_day(back, inst)
                ok = ok and back >= inst.calendar_from
                if count < SMALL.efficiency_lookback_sessions:
                    oldest = min(oldest, _cash_open(back, inst))
                if count < SMALL.overnight_lookback_sessions:
                    oldest = min(oldest, _night_open(back, inst))
            if ok:
                span = (_decision(inst, day).date() - oldest.date()).days
                if worst is None or span > worst[0]:
                    worst = (span, day)
        day += timedelta(days=1)
    assert worst is not None
    assert worst[0] == need
    day = worst[1]
    bars = trend_day(inst, day)
    decided = _decision(inst, day)
    enough = [b for b in bars if b.ts >= _utc_day_start(decided.date() - timedelta(days=need))]
    short = [b for b in bars if b.ts >= _utc_day_start(decided.date() - timedelta(days=need - 1))]
    assert caller(profiles).call(enough, decided).cause is PlayCause.TREND
    assert caller(profiles).call(short, decided).cause is PlayCause.NO_DATA


# --- mutation survivors (qa/mutation/playcaller-wire-v1.notes.md) --------------------


def test_history_reaching_exactly_calendar_from_is_not_warm_up(
    profiles: Profiles, inst: InstrumentProfile
) -> None:
    """#27: the reader refuses only days BEFORE calendar_from. With calendar_from on a
    trading day, the day whose oldest history day is calendar_from itself is labelled;
    the day before it is warm-up (the repo's calendar_from is a holiday, so it cannot
    show this)."""
    start = date(2026, 1, 5)  # a Monday
    moved = replace(inst, calendar_from=start)
    sessions = caller(profiles).history_sessions
    day = start
    for _ in range(sessions - 1):  # the trading day whose oldest history day is `start`
        day += timedelta(days=1)
        while day.weekday() >= 5 or day in inst.holidays:
            day += timedelta(days=1)
    play = PlayCaller(SMALL, PLAY, profiles.strategy.data, moved)
    call = play.call(trend_day(moved, day), _decision(moved, day))
    assert call.cause is PlayCause.TREND
    reader = RegimeReader(SMALL, profiles.strategy.data, moved)
    assert reader.call(trend_day(moved, day), _decision(moved, day)).regime is Regime.TREND
    before = previous_trading_day(day, moved)
    assert (
        PlayCaller(SMALL, PLAY, profiles.strategy.data, moved)
        .call((), _decision(moved, before))
        .cause
        is PlayCause.WARM_UP
    )


def _need_by_hand(profiles: Profiles) -> int:
    """history_days_needed recomputed from the reader's reading list, here."""
    inst, r = profiles.instrument, profiles.regime
    sessions = max(r.efficiency_lookback_sessions, r.overnight_lookback_sessions) + 1
    worst = 0
    day = inst.calendar_from
    while day <= inst.calendar_through:
        if day.weekday() < 5 and day not in inst.holidays and day not in inst.no_trade_days:
            back, ok, oldest = day, True, _night_open(day, inst)
            for count in range(sessions - 1):
                back = previous_trading_day(back, inst)
                ok = ok and back >= inst.calendar_from
                if count < r.efficiency_lookback_sessions:
                    oldest = min(oldest, _cash_open(back, inst))
                if count < r.overnight_lookback_sessions:
                    oldest = min(oldest, _night_open(back, inst))
            if ok:
                decided = _cash_open(day, inst) + MINUTE * r.decision_minutes
                worst = max(worst, (decided.date() - oldest.date()).days)
        day += timedelta(days=1)
    return worst


@pytest.mark.parametrize(
    ("efficiency", "overnight"),
    [(6, 4), (4, 6), (5, 5), (60, 20), (20, 60)],
)
def test_the_need_matches_the_readers_reading_list(
    profiles: Profiles, efficiency: int, overnight: int
) -> None:
    """#29, #33: with the overnight lookback the longer one, the oldest bar read is a
    NIGHT, which opens the calendar day before its trading day."""
    regime = replace(
        profiles.regime,
        efficiency_lookback_sessions=efficiency,
        overnight_lookback_sessions=overnight,
    )
    p = replace(profiles, regime=regime)
    assert history_days_needed(p) == _need_by_hand(p)
