"""The scheduled news, opening-bell and Fed windows (Andre's N1-N7 and the N-OPEN defaults,
2026-10-14; config/news.yaml, config/fomc.yaml, docs/decisions-pending.md).

Every window is a NewsBlackout, so it does what any blackout does, through the paths that
already exist: the risk layer refuses new entries while one is in force (pipeline and
executor), and trade management closes a position held when one starts (ExitReason.NEWS:
the resting stop's cancel confirmed, then a limit sell; no second order path). The
backtest and the live program both add these windows to the CSV ones through
`with_news_rules`, so the two cannot differ.

- N1/N3: each daily event (config) is a window from `lead_minutes` before it to
  `lag_minutes` after it (N2), every calendar day: no weekday rule (N7), no calendar, no
  expiry. A window includes every second of its end minute, as sessions do.
- N4 with N-OPEN-1 (DEFAULT, pending Andre): every day, from the prime session's start
  to the end of the `pre_news_until` event's window.
- N-OPEN-3 (DEFAULT, pending Andre): from `overnight_flat_from` to the overnight
  session's end (its whole end minute).
- N5: on an FOMC decision day, from `fomc_flat_from` to the prime session's close of
  that day (an early close included; nothing when the close comes first).
- N6: the FOMC dates come from the Federal Reserve's pages only. An UNVERIFIED date,
  a cancelled meeting's decision day and every intermeeting action day (an unscheduled
  meeting, a notation vote, or the day one was announced) are blackouts all day; so
  is every day outside the calendar's valid_from .. valid_through (fail closed, like
  the exchange calendar), and live alarms it
  (`fomc_warning`, "FOMC CALENDAR EXPIRED" is critical).
- N-OPEN-2 (DEFAULT, pending Andre): the daily windows (N1, N3, N4) start the news time
  stop only when `time_stop_after_daily` is on; every other window always does.

Times are config; this module knows no clock time, zone or length of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from footprint.core.calendar import prime_close
from footprint.core.config import (
    ConfigError,
    DailyEvent,
    FomcAction,
    FomcMeeting,
    InstrumentProfile,
    NewsRulesSettings,
    Profiles,
    Session,
)
from footprint.core.models import require_utc
from footprint.decision.session import NewsBlackout

_DAY = timedelta(days=1)
_MINUTE = timedelta(minutes=1)
OVERNIGHT_NAME = "overnight flat window (N-OPEN-3)"
EXPIRED_MARKER = "FOMC CALENDAR EXPIRED"


def _utc(day: date, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, zone).astimezone(UTC)


def _end_of_minute(moment: datetime) -> datetime:
    """The last instant of `moment`'s minute: a window covers its whole end minute."""
    return moment + _MINUTE - timedelta.resolution


def _covers(session: Session, clock: time) -> bool:
    if session.start <= session.end:
        return session.start <= clock <= session.end
    return clock >= session.start or clock <= session.end


def pre_news_name(event: str) -> str:
    return f"no entries before news (N4) until {event}"


class NewsRules:
    def __init__(self, settings: NewsRulesSettings, instrument: InstrumentProfile) -> None:
        """Refuses (ConfigError) rules that could never be in force as written: an
        overnight flat window starting outside the overnight session or at its last
        minute, or an N4 window ending before it starts on any day of the maintained
        calendar."""
        self._s = settings
        self._instrument = instrument
        self._zone = ZoneInfo(settings.timezone)
        self._cache: dict[date, tuple[NewsBlackout, ...]] = {}
        overnight = instrument.overnight_session
        start = settings.overnight_flat_from
        if not _covers(overnight, start) or start == overnight.end:
            raise ConfigError(
                f"news rules: overnight_flat_from {start} must start inside the overnight "
                f"session ({overnight.start} to {overnight.end}), before its last minute "
                "(N-OPEN-3)"
            )
        self._until = next(e for e in settings.daily_events if e.name == settings.pre_news_until)
        day = instrument.calendar_from
        while day <= instrument.calendar_through:
            self._pre_news(day)  # raises on an empty window
            day += _DAY
        fomc = settings.fomc
        self._decisions = {m.decision_day: m for m in fomc.meetings}
        self._cancelled = frozenset(m.decision_day for m in fomc.cancelled)
        self._actions: dict[date, list[FomcAction]] = {}
        for action in fomc.intermeeting:
            self._actions.setdefault(action.day, []).append(action)
        for m in fomc.meetings:
            if instrument.early_closes.get(m.decision_day) is None and self._fomc(m) is None:
                raise ConfigError(
                    f"news rules: fomc_flat_from {settings.fomc_flat_from} is not before the "
                    f"prime close on {m.decision_day}: N5 could never be in force"
                )

    def _daily(self, day: date, event: DailyEvent) -> NewsBlackout:
        at = _utc(day, event.at, self._zone)
        return NewsBlackout(
            event.name,
            at - _MINUTE * self._s.lead_minutes,
            _end_of_minute(at + _MINUTE * self._s.lag_minutes),
            time_stop=self._s.time_stop_after_daily,
        )

    def _pre_news(self, day: date) -> NewsBlackout:
        prime = self._instrument.prime_session
        start = _utc(day, prime.start, ZoneInfo(prime.timezone))
        end = self._daily(day, self._until).end
        if end < start:
            raise ConfigError(
                f"news rules: pre_news_until {self._until.name!r} ends before the prime "
                f"session starts ({prime.start}) on {day}: the N4 window would be empty"
            )
        return NewsBlackout(
            pre_news_name(self._until.name),
            start,
            end,
            time_stop=self._s.time_stop_after_daily,
        )

    def _overnight(self, day: date) -> NewsBlackout:
        session = self._instrument.overnight_session
        zone = ZoneInfo(session.timezone)
        start = self._s.overnight_flat_from
        ends_on = day + _DAY if start > session.end else day
        return NewsBlackout(
            OVERNIGHT_NAME,
            _utc(day, start, zone),
            _end_of_minute(_utc(ends_on, session.end, zone)),
        )

    def _whole_day(self, day: date, name: str) -> NewsBlackout:
        start = _utc(day, time.min, self._zone)
        return NewsBlackout(
            name, start, _utc(day + _DAY, time.min, self._zone) - timedelta.resolution
        )

    def _fomc(self, meeting: FomcMeeting) -> NewsBlackout | None:
        """N5 on a decision day: None when the prime session closes first."""
        day = meeting.decision_day
        prime = self._instrument.prime_session
        start = _utc(day, self._s.fomc_flat_from, self._zone)
        close = _utc(day, prime_close(day, self._instrument), ZoneInfo(prime.timezone))
        if start >= close:
            return None
        return NewsBlackout(f"FOMC decision day {day} (N5)", start, _end_of_minute(close))

    def _fed(self, day: date) -> tuple[NewsBlackout, ...]:
        fomc = self._s.fomc
        if day > fomc.valid_through:
            return (
                self._whole_day(
                    day, f"{EXPIRED_MARKER} on {fomc.valid_through} (N6): no new entries"
                ),
            )
        if day < fomc.valid_from:
            return (
                self._whole_day(
                    day, f"FOMC calendar starts {fomc.valid_from} (N6): no new entries"
                ),
            )
        found: list[NewsBlackout] = []
        for action in self._actions.get(day, ()):
            flag = "" if action.verified else " UNVERIFIED"
            found.append(
                self._whole_day(
                    day, f"FOMC intermeeting action {day}{flag} (N6): no new entries all day"
                )
            )
        if day in self._cancelled:
            found.append(
                self._whole_day(day, f"FOMC meeting {day} cancelled (N6): no new entries all day")
            )
        meeting = self._decisions.get(day)
        if meeting is not None and not meeting.verified:
            found.append(
                self._whole_day(day, f"FOMC date {day} UNVERIFIED (N6): no new entries all day")
            )
        elif meeting is not None:
            window = self._fomc(meeting)
            if window is not None:
                found.append(window)
        return tuple(found)

    def day(self, day: date) -> tuple[NewsBlackout, ...]:
        """The windows that begin on `day` (on each rule's own clock), built once."""
        found = self._cache.get(day)
        if found is None:
            found = (
                *(self._daily(day, event) for event in self._s.daily_events),
                self._pre_news(day),
                self._overnight(day),
                *self._fed(day),
            )
            self._cache[day] = found
        return found

    def windows(self, now: datetime) -> tuple[NewsBlackout, ...]:
        """Every window that can be in force at `now`, or ended just before it (the
        news time stop looks back at the last one): those of the day before, the day
        and the day after `now` on the news clock."""
        require_utc(now, "now")
        today = now.astimezone(self._zone).date()
        return (*self.day(today - _DAY), *self.day(today), *self.day(today + _DAY))


def with_news_rules(
    profiles: Profiles, extra: Callable[[datetime], Iterable[NewsBlackout]]
) -> Callable[[datetime], tuple[NewsBlackout, ...]]:
    """The blackouts the runner reads: the scheduled rules, then `extra` (the CSV). A
    failure in either is the runner's to handle (an unreadable calendar is a blackout
    in force)."""
    rules = NewsRules(profiles.news_rules, profiles.instrument)

    def blackouts(now: datetime) -> tuple[NewsBlackout, ...]:
        return (*rules.windows(now), *extra(now))

    return blackouts


def fomc_warning(today: date, settings: NewsRulesSettings) -> str | None:
    """What to tell Andre, once a day, about the FOMC calendar (N6): expired (critical:
    no new entries until it is extended), running out, or an UNVERIFIED date ahead."""
    fomc = settings.fomc
    if today > fomc.valid_through:
        return (
            f"{EXPIRED_MARKER} on {fomc.valid_through}: NO NEW TRADES (every later day is a "
            "blackout, N6) until config/fomc.yaml is extended from the Federal Reserve's "
            "published calendar and the program is restarted"
        )
    horizon = today + timedelta(days=settings.fomc_warn_days)
    if fomc.valid_through <= horizon:
        left = (fomc.valid_through - today).days
        return (
            f"FOMC calendar ends {fomc.valid_through} ({left} days): add the next meetings "
            "from the Federal Reserve's published calendar to config/fomc.yaml"
        )
    ahead = sorted(
        [m.decision_day for m in fomc.meetings if not m.verified]
        + [a.day for a in fomc.intermeeting if not a.verified]
    )
    ahead = [day for day in ahead if today <= day <= horizon]
    if ahead:
        return (
            f"FOMC date {ahead[0]} is UNVERIFIED on the Federal Reserve's calendar: "
            "that whole day is a blackout until config/fomc.yaml marks it verified (N6)"
        )
    return None


def check_fomc_span(profiles: Profiles, first: datetime, last: datetime) -> None:
    """A backtest must trade inside the FOMC calendar (N6, like D34): outside it every
    day is a blackout, so a run there would trade nothing without an error. Raises
    ValueError."""
    s = profiles.news_rules
    zone = ZoneInfo(s.timezone)
    start, end = first.astimezone(zone).date(), last.astimezone(zone).date()
    if start < s.fomc.valid_from:
        raise ValueError(
            f"the backtest trades from {start}, but the FOMC calendar starts {s.fomc.valid_from} "
            "(N6): every day before it is a blackout. Add the earlier meetings from the "
            "Federal Reserve's own calendar pages to config/fomc.yaml first"
        )
    if end > s.fomc.valid_through:
        raise ValueError(
            f"the backtest trades until {end}, past the FOMC calendar's valid_through "
            f"{s.fomc.valid_through} (N6): every later day is a blackout. Extend "
            "config/fomc.yaml from the Federal Reserve's calendar first"
        )
