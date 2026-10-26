"""The scheduled news and opening-bell windows (Andre's N1-N4, N7 and the N-OPEN defaults,
2026-10-14; config/news.yaml, docs/decisions-pending.md).

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
- N-OPEN-2 (DEFAULT, pending Andre): each daily window (N1, N3) starts the news time
  stop only when its own `time_stop` switch is on, and N4 only when `pre_news_time_stop`
  is; every other window (overnight, CSV) always does.

Times are config; this module knows no clock time, zone or length of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from footprint.core.config import (
    ConfigError,
    DailyEvent,
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

    def _daily(self, day: date, event: DailyEvent) -> NewsBlackout:
        at = _utc(day, event.at, self._zone)
        return NewsBlackout(
            event.name,
            at - _MINUTE * self._s.lead_minutes,
            _end_of_minute(at + _MINUTE * self._s.lag_minutes),
            time_stop=event.time_stop,
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
            time_stop=self._s.pre_news_time_stop,
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

    def day(self, day: date) -> tuple[NewsBlackout, ...]:
        """The windows that begin on `day` (on each rule's own clock), built once."""
        found = self._cache.get(day)
        if found is None:
            found = (
                *(self._daily(day, event) for event in self._s.daily_events),
                self._pre_news(day),
                self._overnight(day),
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


def _clock_text(clock: time, shift: timedelta = timedelta(0)) -> str:
    """`clock` moved by `shift`, as HH:MM (a lead or lag is shorter than a day)."""
    return (datetime.combine(date.min + _DAY, clock) + shift).strftime("%H:%M")


def _stop_text(on: bool) -> str:
    return "time stop after: " + ("on" if on else "off")


def news_assumptions(profiles: Profiles, source: Path) -> dict[str, str]:
    """The backtest readout's record of the scheduled rules in force (review round 1
    C3): the file, each value it was read with, and every window as built, so runs on
    different news.yaml values can be told apart."""
    s = profiles.news_rules
    lead, lag = _MINUTE * s.lead_minutes, _MINUTE * s.lag_minutes
    prime = profiles.instrument.prime_session
    night = profiles.instrument.overnight_session
    until = next(e for e in s.daily_events if e.name == s.pre_news_until)
    lines = {
        "news rules": f"{source} (always on, every calendar day, beside any CSV)",
        "news.timezone": s.timezone,
        "news.lead_minutes": str(s.lead_minutes),
        "news.lag_minutes": str(s.lag_minutes),
        "news.pre_news_until": s.pre_news_until,
        "news.pre_news_time_stop": "true" if s.pre_news_time_stop else "false",
        "news.overnight_flat_from": _clock_text(s.overnight_flat_from),
    }
    for e in s.daily_events:
        lines[f"news window {e.name}"] = (
            f"{_clock_text(e.at, -lead)}-{_clock_text(e.at, lag)} {s.timezone} "
            f"(whole end minute); {_stop_text(e.time_stop)}"
        )
    lines[f"news window {pre_news_name(until.name)}"] = (
        f"{_clock_text(prime.start)} {prime.timezone}-{_clock_text(until.at, lag)} "
        f"{s.timezone} (whole end minute); {_stop_text(s.pre_news_time_stop)}"
    )
    lines[f"news window {OVERNIGHT_NAME}"] = (
        f"{_clock_text(s.overnight_flat_from)}-{_clock_text(night.end)} {night.timezone} "
        f"(whole end minute); {_stop_text(True)}"
    )
    return lines


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
