"""The play caller: which play runs today, from the regime reader's label.

Andre's decisions (docs/decisions-pending.md, play caller):
- PC-5 (2026-10-22): a TREND day runs the existing breakout entry (the live momentum
  entry, unchanged); a CHOP day and a STAND ASIDE day take no new entries.
- PC-4: only a label made on time counts. The on-time window is `play.on_time_seconds`
  from the decision time (regime.decision_minutes after the cash open). Inside it:
  - while the opening's final minute has not arrived yet (no closed bar at or after it
    among the bars read), nothing is fixed: the play is WAITING (no new entries) and the
    next decision asks again (review round 1, M1: a bar arriving late but inside the
    window is not a late label);
  - otherwise the reader is asked and its label stands for the rest of the trading day,
    whatever it is (a data failure included: with a later minute already there, the
    final minute is missing, not late, and it is not retried).
  After the window, the reader is never asked: a day still WAITING is fixed NO_DATA (the
  final minute did not come on time); a day never asked inside it (data late, the broker
  away, a restart after the decision minute) is LATE. A bad overnight stands aside (the
  reader's own rule).
- No new entries before the decision time, on any day: the pre-cash prime hours and the
  first `decision_minutes` of the cash session take no new entries. The reader is not
  asked then, so nothing is fixed before the decision minute.
- PC-3: a day whose history would reach before `calendar_from` cannot be labelled (the
  reader refuses it): WARM_UP, stand aside. The reader is not asked.
- Exits on an open position never depend on the label: the label only gates the entry
  decision, which the runner asks for only with nothing open or working.

The reader reads `history_sessions` trading days of 1-minute bars (PC-2). Live, the bars
handed to the decision are market data's whole spliced history, so
`market_data.history_days` must cover them; `check_live_history` refuses a start
otherwise. The backtest hands the play caller its own source: every bar that has closed,
from the start of the data (it is read once a day, so the cost is one pass a day).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Protocol
from zoneinfo import ZoneInfo

from footprint.agents.regime_reader import Regime, RegimeCall, RegimeReader
from footprint.core.calendar import (
    SessionKind,
    is_tradable_day,
    previous_trading_day,
    session_at,
    trading_day_at,
)
from footprint.core.config import (
    DataSettings,
    InstrumentProfile,
    PlaySettings,
    Profiles,
    RegimeSettings,
)
from footprint.core.models import Bar, require_utc

_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)


class PlayCause(Enum):
    """Why the day has the play it has. Only TREND allows new entries."""

    TREND = "trend"  # TREND label: the breakout entry runs
    CHOP = "chop"  # CHOP label: no new entries (no chop play is built)
    RULES = "rules"  # the rules called it neither trend nor chop: stand aside
    NO_DATA = "no_data"  # the reader could not use the data on time: stand aside
    WARM_UP = "warm_up"  # the history would reach before calendar_from (PC-3)
    LATE = "late"  # no call inside the on-time window (PC-4)
    READER_FAILED = "reader_failed"  # the reader or its history source raised
    BEFORE_DECISION = "before_decision"  # prime, before the decision time (nothing fixed)
    WAITING = "waiting"  # on time, the opening's final minute not there yet (nothing fixed)
    OUTSIDE_PRIME = "outside_prime"  # not the prime session (nothing fixed)


@dataclass(frozen=True, slots=True)
class PlayCall:
    """The play for the moment asked. `first` is True only on the call that fixed the
    day's label (the journal records the day's label once, from it)."""

    day: date | None
    regime: Regime
    cause: PlayCause
    reason: str
    label: RegimeCall | None = None  # the reader's call that fixed the day, if asked
    first: bool = False

    def __post_init__(self) -> None:
        if (self.cause is PlayCause.TREND) != (self.regime is Regime.TREND):
            raise ValueError("only a TREND label is a TREND play")
        if (self.cause is PlayCause.CHOP) != (self.regime is Regime.CHOP):
            raise ValueError("only a CHOP label is a CHOP play")

    @property
    def entries_allowed(self) -> bool:
        return self.cause is PlayCause.TREND


class PlayGate(Protocol):
    """The play caller's job, as the pipeline sees it."""

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> PlayCall: ...


class PlayCaller:
    def __init__(
        self,
        regime: RegimeSettings,
        play: PlaySettings,
        data: DataSettings,
        instrument: InstrumentProfile,
        history: Callable[[datetime], Sequence[Bar]] | None = None,
    ) -> None:
        """`history`: the reader's own bars at a moment; None reads the bars handed to
        `call` (live: market data's whole history)."""
        self._reader = RegimeReader(regime, data, instrument)
        self._regime = regime
        self._on_time = timedelta(seconds=play.on_time_seconds)
        self._instrument = instrument
        self._zone = ZoneInfo(instrument.cash_session.timezone)
        self._history = history
        self._fixed: tuple[date, PlayCall] | None = None
        self._waiting: date | None = None  # the day a WAITING call was last given for

    @property
    def agent_id(self) -> str:
        return "play"

    @property
    def history_sessions(self) -> int:
        return self._reader.history_sessions

    def decision_time(self, day: date) -> datetime:
        """When `day`'s label is made: decision_minutes after its cash open (UTC)."""
        start = self._instrument.cash_session.start
        cash_open = datetime.combine(day, start, self._zone).astimezone(UTC)
        return cash_open + _MINUTE * self._regime.decision_minutes

    def call(self, minute_bars: Sequence[Bar], as_of: datetime) -> PlayCall:
        require_utc(as_of, "as_of")
        today = trading_day_at(as_of, self._instrument)
        if today is None or session_at(as_of, self._instrument) is not SessionKind.PRIME:
            return PlayCall(
                None, Regime.STAND_ASIDE, PlayCause.OUTSIDE_PRIME, "outside the prime session"
            )
        if self._fixed is not None and self._fixed[0] == today:
            return replace(self._fixed[1], first=False)
        due = self.decision_time(today)
        if as_of < due:
            return PlayCall(
                today,
                Regime.STAND_ASIDE,
                PlayCause.BEFORE_DECISION,
                f"no new entries before the decision time {due.isoformat()}",
            )
        fixed = self._fix(minute_bars, as_of, today, due)
        if fixed.cause is PlayCause.WAITING:
            self._waiting = today
            return fixed
        self._fixed = (today, fixed)
        return fixed

    def _fix(
        self, minute_bars: Sequence[Bar], as_of: datetime, today: date, due: datetime
    ) -> PlayCall:
        def aside(cause: PlayCause, reason: str, label: RegimeCall | None = None) -> PlayCall:
            return PlayCall(today, Regime.STAND_ASIDE, cause, reason, label, first=True)

        final = due - _MINUTE  # the opening's final minute (a bar's ts is its start)
        if as_of >= due + self._on_time:
            if self._waiting == today:
                return aside(
                    PlayCause.NO_DATA,
                    f"the opening's final minute ({final.isoformat()}) had not arrived by "
                    f"the end of the on-time window ({self._on_time} after "
                    f"{due.isoformat()}); no new entries today",
                )
            return aside(
                PlayCause.LATE,
                f"no label on time: the first decision at or after {due.isoformat()} came at "
                f"{as_of.isoformat()} (due within {self._on_time}); no new entries today",
            )
        if warm_up(today, self.history_sessions, self._instrument):
            return aside(
                PlayCause.WARM_UP,
                f"warm-up: {self.history_sessions} sessions back from {today.isoformat()} "
                f"reach before calendar_from {self._instrument.calendar_from.isoformat()}; "
                "no new entries today",
            )
        try:
            bars = minute_bars if self._history is None else self._history(as_of)
            if not _has_closed_bar_from(bars, final, as_of):
                return PlayCall(
                    today,
                    Regime.STAND_ASIDE,
                    PlayCause.WAITING,
                    f"waiting for the opening's final minute ({final.isoformat()}): no "
                    "new entries yet",
                )
            label = self._reader.call(bars, as_of)
        except (ValueError, TypeError) as exc:
            return aside(
                PlayCause.READER_FAILED,
                f"the regime reader failed: {exc}; no new entries today",
            )
        if label.regime is Regime.TREND:
            return PlayCall(
                today,
                Regime.TREND,
                PlayCause.TREND,
                f"TREND day ({label.reason}): the breakout entry runs",
                label,
                first=True,
            )
        if label.regime is Regime.CHOP:
            return PlayCall(
                today,
                Regime.CHOP,
                PlayCause.CHOP,
                f"CHOP day ({label.reason}): no new entries (no chop play)",
                label,
                first=True,
            )
        # A label the rules made always carries its rank; a data failure never does.
        cause = PlayCause.NO_DATA if label.efficiency_rank_pct is None else PlayCause.RULES
        return aside(cause, f"stand aside: {label.reason}; no new entries today", label)


def _has_closed_bar_from(bars: Sequence[Bar], start: datetime, as_of: datetime) -> bool:
    """True when the newest bar closed by `as_of` starts at or after `start` (bars in
    time order; out-of-order input is the reader's to refuse)."""
    for bar in reversed(bars):
        if bar.ts + _MINUTE <= as_of:
            return bar.ts >= start
    return False


def warm_up(day: date, sessions: int, instrument: InstrumentProfile) -> bool:
    """True when the `sessions - 1` trading days before `day` reach before calendar_from
    (the reader refuses such history: PC-3)."""
    back = day
    for _ in range(sessions - 1):
        back = previous_trading_day(back, instrument)
        if back < instrument.calendar_from:
            return True
    return False


class PlayRefused(ValueError):
    """The play caller cannot run on this configuration: refuse to start."""


class PlayHistoryTooShort(PlayRefused):
    """The configured history cannot cover the regime reader: refuse to start."""


def history_days_needed(profiles: Profiles) -> int:
    """Calendar days of history live market data must keep (it keeps bars from 00:00 UTC
    `history_days` days before today's UTC date) so the reader has every bar it reads on
    every labellable day of the maintained calendar: today's night and opening, the
    openings of the previous efficiency_lookback_sessions days and the nights of the
    previous overnight_lookback_sessions days. Warm-up days (PC-3) are skipped."""
    p, r = profiles.instrument, profiles.regime
    try:
        caller = PlayCaller(r, profiles.play, profiles.strategy.data, p)
    except ValueError as exc:  # the reader's own check of the regime settings
        raise PlayRefused(
            f"the regime config cannot be used: {exc} (config/regime.yaml). Refusing to start"
        ) from exc
    zone = ZoneInfo(p.cash_session.timezone)
    night = p.overnight_session
    night_zone = ZoneInfo(night.timezone)

    def cash_open(day: date) -> datetime:
        return datetime.combine(day, p.cash_session.start, zone).astimezone(UTC)

    def night_open(day: date) -> datetime:
        start_day = day - _DAY if night.start > night.end else day
        return datetime.combine(start_day, night.start, night_zone).astimezone(UTC)

    need: int | None = None
    day = p.calendar_from
    while day <= p.calendar_through:
        if is_tradable_day(day, p) and not warm_up(day, caller.history_sessions, p):
            oldest, back = night_open(day), day
            for count in range(caller.history_sessions - 1):
                back = previous_trading_day(back, p)
                if count < r.efficiency_lookback_sessions:
                    oldest = min(oldest, cash_open(back))
                if count < r.overnight_lookback_sessions:
                    oldest = min(oldest, night_open(back))
            span = (caller.decision_time(day).date() - oldest.date()).days
            need = span if need is None else max(need, span)
        day += _DAY
    if need is None:
        raise PlayHistoryTooShort(
            f"no day of the maintained calendar ({p.calendar_from} to {p.calendar_through}) "
            f"has {caller.history_sessions} sessions inside it: the regime reader can never "
            "label a day. Extend the calendar or shorten the regime lookbacks"
        )
    return need


def check_live_history(profiles: Profiles) -> None:
    """Live start-up check (PC-2): fail closed when market_data.history_days cannot
    cover the regime reader on some day of the maintained calendar (PlayHistoryTooShort),
    or the regime settings cannot be used at all (PlayRefused)."""
    need = history_days_needed(profiles)
    have = profiles.execution.market_data.history_days
    if have < need:
        sessions = max(
            profiles.regime.efficiency_lookback_sessions,
            profiles.regime.overnight_lookback_sessions,
        )
        raise PlayHistoryTooShort(
            f"market_data.history_days is {have}, but the play caller's regime reader needs "
            f"{need} calendar days of 1-minute bars ({sessions} sessions back from the "
            f"worst day of the calendar). Raise market_data.history_days to at least {need} "
            "(config/execution.yaml). Refusing to start"
        )
