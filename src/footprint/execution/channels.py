"""Where alarms reach Andre (decision D20): Telegram push for every alarm, SMS when
Telegram fails, and a phone call for the critical few.

Each sink is a callable for an AlarmLine (execution/alarms.py): it delivers one alarm
or raises, and the line retries (never blocking the trading loop). A sink raises
`ChannelError` with a message that never contains a credential: a token in an error,
a log or an alarm would leak it. Every send has a hard deadline
(`channel_timeout_seconds`): a request that hangs is abandoned and counts as failed.

    TelegramSink   POST api.telegram.org/bot<token>/sendMessage
    TwilioSmsSink  POST api.twilio.com/2010-04-01/Accounts/<sid>/Messages.json
    TwilioCallSink POST api.twilio.com/2010-04-01/Accounts/<sid>/Calls.json (spoken)

`is_critical` decides which alarms also ring the phone. `CriticalCalls` rings each
kind at most once per cooldown and never retries or waits on a failed call: a failed
or hung call never holds back a later alarm.

Each channel is watched (`ChannelHealth`): the first failure raises "ALARM CHANNEL
DOWN: <channel>" (critical) to the record (alarms.log) and every OTHER channel, so a
dead phone line is reported by Telegram and SMS; the first success after that says
it is back. Andre's remediation items 3 and 10.

Modes (AlarmHub.mode): FULL (all six keys), or DEGRADED_ALERTING (Telegram only, or
local only), chosen by an explicit start-up flag and never shown as full coverage.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from http import HTTPStatus
from xml.sax.saxutils import escape

from footprint.core.config import AlarmSettings
from footprint.execution.alarms import AlarmLine
from footprint.execution.management.malik import Alarm

log = logging.getLogger("footprint.channels")

# What makes an alarm worth a phone call. Each marker is checked against the source by
# a test, so renaming an alarm cannot silently stop the call.
CRITICAL_MARKERS: tuple[str, ...] = (
    "HALT:",
    "KILL SWITCH LATCHED",
    "could not save the kill-switch latch",
    "flatten by hand",
    "CANCEL BY HAND",
    "CANCEL STUCK",
    "BROKER REPORT NOT SETTLING",
    "unprotected",
    # A position without its resting stop, and the failsafe flattening one nothing
    # manages (independent audit A-4; markers are case-sensitive).
    "filled without a resting stop",
    "no resting stop",
    "unmanaged since",
    "IBKR CONNECTION REFUSED",
    "IBKR SESSION KEEPS FAILING",
    "resync is not completing",
    "EXCHANGE CALENDAR EXPIRED",
    "market data step failed",
    "that data is missing",  # MARKET DATA: <bars, chain, history...> failed
    "could not read the held contracts",
    "cannot stream the held",
    "no future to trade",
    "IBKR connection lost",
    "ALARM CHANNEL DOWN",
    "UNATTENDED",
    "QUOTE FEED SILENT",
)

_TELEGRAM = "https://api.telegram.org"
_TWILIO = "https://api.twilio.com/2010-04-01/Accounts"
_TELEGRAM_MAX = 4096  # Telegram's limit on one message's text
_SMS_MAX = 1600  # Twilio's limit on one message body
_SPOKEN_MAX = 400  # a call reads out at most this much, then says to check the phone

Post = Callable[[str, Mapping[str, str], tuple[str, str] | None, float], int]


class ChannelError(RuntimeError):
    """A delivery failed. The message names the channel and the HTTP status only."""


def within[T](seconds: float, action: Callable[[], T], busy: threading.Event | None = None) -> T:
    """`action()`, or ChannelError if it has not answered within `seconds` (it is left
    to finish on its own daemon thread, never waited for). Any other error becomes a
    ChannelError naming only its type.

    `busy` (one per channel) bounds what a dead channel can pile up: while an earlier
    send of the channel is still running, the next one fails at once instead of
    starting another thread, so a channel never holds more than one abandoned send."""
    if busy is not None:
        if busy.is_set():
            raise ChannelError("the previous send has not answered yet")
        busy.set()
    box: list[tuple[bool, object]] = []

    def run() -> None:
        try:
            box.append((True, action()))
        except BaseException as exc:  # carried back to the caller
            box.append((False, exc))
        finally:
            if busy is not None:
                busy.clear()

    worker = threading.Thread(target=run, name="alarm-send", daemon=True)
    worker.start()
    worker.join(seconds)
    if not box:
        raise ChannelError(f"no answer within {seconds:g}s")
    ok, value = box[0]
    if ok:
        return value  # type: ignore[return-value]
    if isinstance(value, ChannelError):
        raise ChannelError(str(value)) from None
    raise ChannelError(f"failed ({type(value).__name__})") from None


def http_post(
    url: str, data: Mapping[str, str], auth: tuple[str, str] | None, timeout: float
) -> int:
    """POST a form; the HTTP status. Raises ChannelError without the URL (it can hold a
    token)."""
    body = urllib.parse.urlencode(dict(data)).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth is not None:
        pair = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {pair}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception as exc:  # anything: some (http.client.InvalidURL) quote the URL
        reason = type(exc.reason if isinstance(exc, urllib.error.URLError) else exc).__name__
        raise ChannelError(f"no answer ({reason})") from None


def critical_marker(message: str) -> str | None:
    """The first critical marker in the message, or None."""
    return next((marker for marker in CRITICAL_MARKERS if marker in message), None)


# Kinds that name their subject (the channel): one channel down never silences another.
_PER_SUBJECT = "ALARM CHANNEL DOWN"


def critical_kind(message: str) -> str | None:
    """What a critical alarm's call cooldown is kept by: its marker, or for a channel
    alert the marker and the channel ("ALARM CHANNEL DOWN: SMS")."""
    marker = critical_marker(message)
    if marker != _PER_SUBJECT:
        return marker
    subject = message.split(marker, 1)[1].removeprefix(":").strip().split(" failed", 1)[0]
    return f"{marker}: {subject}"


def is_critical(message: str) -> bool:
    return critical_marker(message) is not None


def _text(alarm: Alarm) -> str:
    return f"FOOTPRINT {alarm.ts:%Y-%m-%d %H:%M:%S}Z\n{alarm.message}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ok(status: int, channel: str) -> None:
    if not (status in HTTPStatus.__members__.values() and HTTPStatus(status).is_success):
        raise ChannelError(f"{channel} answered HTTP {status}")


@dataclass(frozen=True)
class TelegramSink:
    token: str
    chat_id: str
    timeout: float
    post: Post = http_post
    prefix: str = ""  # e.g. the DEGRADED_ALERTING banner, on every message
    _busy: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False, compare=False
    )

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TELEGRAM}/bot{self.token}/sendMessage"
        text = _clip(self.prefix + _text(alarm), _TELEGRAM_MAX)
        data = {"chat_id": self.chat_id, "text": text}
        try:
            status = within(
                self.timeout, lambda: self.post(url, data, None, self.timeout), self._busy
            )
        except ChannelError as exc:
            raise ChannelError(f"Telegram: {exc}") from None
        _ok(status, "Telegram")

    def __repr__(self) -> str:  # never the token
        return "TelegramSink(<token>, <chat>)"


@dataclass(frozen=True)
class TwilioSmsSink:
    account_sid: str
    auth_token: str
    from_number: str
    to_number: str
    timeout: float
    post: Post = http_post
    _busy: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False, compare=False
    )

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TWILIO}/{self.account_sid}/Messages.json"
        data = {
            "From": self.from_number,
            "To": self.to_number,
            "Body": _clip(_text(alarm), _SMS_MAX),
        }
        try:
            status = within(
                self.timeout,
                lambda: self.post(url, data, (self.account_sid, self.auth_token), self.timeout),
                self._busy,
            )
        except ChannelError as exc:
            raise ChannelError(f"Twilio SMS: {exc}") from None
        _ok(status, "Twilio SMS")

    def __repr__(self) -> str:
        return "TwilioSmsSink(<credentials>)"


@dataclass(frozen=True)
class TwilioCallSink:
    account_sid: str
    auth_token: str
    from_number: str
    to_number: str
    timeout: float
    post: Post = http_post
    _busy: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False, compare=False
    )

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TWILIO}/{self.account_sid}/Calls.json"
        spoken = _clip(alarm.message, _SPOKEN_MAX)
        twiml = (
            "<Response><Say>Footprint critical alarm. "
            f"{escape(spoken)}. Check your phone.</Say></Response>"
        )
        data = {"From": self.from_number, "To": self.to_number, "Twiml": twiml}
        try:
            status = within(
                self.timeout,
                lambda: self.post(url, data, (self.account_sid, self.auth_token), self.timeout),
                self._busy,
            )
        except ChannelError as exc:
            raise ChannelError(f"Twilio call: {exc}") from None
        _ok(status, "Twilio call")

    def __repr__(self) -> str:
        return "TwilioCallSink(<credentials>)"


class CriticalCalls:
    """A sink that rings only for critical alarms, at most once per `cooldown` seconds
    for each kind (marker): a repeating alarm still reaches Telegram and SMS each time,
    and one kind ringing never silences another (a stuck resync must not hold back a
    kill switch).

    A call that fails or hangs (the sink's own deadline) is NOT retried and never
    raises: the next alarm is never held behind it. It starts no cooldown, so the next
    alarm of that kind rings again; the channel's watcher reports it down."""

    def __init__(
        self,
        call: Callable[[Alarm], None],
        cooldown: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._call = call
        self._cooldown = cooldown
        self._monotonic = monotonic
        self._last: dict[str, float] = {}

    def __call__(self, alarm: Alarm) -> None:
        marker = critical_kind(alarm.message)
        if marker is None:
            return
        now = self._monotonic()
        last = self._last.get(marker)
        if last is not None and now - last < self._cooldown:
            log.info("%r not rung again (rung %.0fs ago)", marker, now - last)
            return
        try:
            self._call(alarm)
        except Exception as exc:  # reported by the channel's watcher; never blocks
            log.warning("call not made (%s): not retried", _safe(exc))
            return
        self._last[marker] = now


def _safe(exc: BaseException) -> str:
    """An error as text that cannot carry a credential: a ChannelError's own message
    (built to be safe), otherwise only the type."""
    return str(exc) if isinstance(exc, ChannelError) else type(exc).__name__


class ChannelHealth:
    """Watches one channel's sink. The first failure after working (or at start) tells
    `notify` "ALARM CHANNEL DOWN"; the first success after a failure tells it the
    channel is back. Repeated failures say nothing more (the state shows in `status`).
    Re-raises every failure, so the channel's own line keeps its retry behaviour."""

    def __init__(
        self,
        name: str,
        sink: Callable[[Alarm], None],
        clock: Callable[[], datetime],
    ) -> None:
        self.name = name
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self._down_since: datetime | None = None
        self._failures = 0
        self._notify: Callable[[str, str], None] = lambda channel, message: None

    def watch(self, notify: Callable[[str, str], None]) -> None:
        self._notify = notify

    def __call__(self, alarm: Alarm) -> None:
        try:
            self._sink(alarm)
        except Exception as exc:
            with self._lock:
                first = self._down_since is None
                self._failures += 1
                if first:
                    self._down_since = self._clock()
            if first:
                self._notify(
                    self.name,
                    f"ALARM CHANNEL DOWN: {self.name} failed ({_safe(exc)}). Alarms go by the "
                    "other channels; check the account and the keys",
                )
            raise
        with self._lock:
            was_down, failures = self._down_since is not None, self._failures
            self._down_since, self._failures = None, 0
        if was_down:
            self._notify(self.name, f"alarm channel back: {self.name} ({failures} failed)")

    @property
    def status(self) -> str:
        with self._lock:
            if self._down_since is None:
                return f"{self.name}: ok"
            return (
                f"{self.name}: DOWN since {self._down_since.isoformat()} ({self._failures} failed)"
            )


_PHONE = re.compile(r"\+[1-9]\d{7,14}", re.ASCII)  # E.164
_SHAPES: dict[str, re.Pattern[str]] = {
    "TELEGRAM_BOT_TOKEN": re.compile(r"\d+:[A-Za-z0-9_-]+", re.ASCII),
    "TELEGRAM_CHAT_ID": re.compile(r"-?\d+", re.ASCII),
    "TWILIO_ACCOUNT_SID": re.compile(r"AC[0-9a-fA-F]{32}", re.ASCII),
    "TWILIO_AUTH_TOKEN": re.compile(r"[A-Za-z0-9]+", re.ASCII),
    "TWILIO_FROM_NUMBER": _PHONE,
    "ALARM_PHONE_NUMBER": _PHONE,
}


def _read_keys(env: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    """The keys' values in their expected shape, or ValueError naming the missing or
    malformed ones (never a value). A stray space or an inline comment would otherwise
    go into a URL."""
    missing = [k for k in keys if not env.get(k, "").strip()]
    if missing:
        raise ValueError(f"missing in .env: {', '.join(missing)}")
    values = [env[k].strip() for k in keys]
    bad = [k for k, v in zip(keys, values, strict=True) if not _SHAPES[k].fullmatch(v)]
    if bad:
        raise ValueError(
            f"malformed in .env (no spaces or comments after the value): {', '.join(bad)}"
        )
    return values


@dataclass(frozen=True)
class ChannelSettings:
    """Full alerting: all six keys from .env."""

    telegram_token: str
    telegram_chat_id: str
    twilio_sid: str
    twilio_token: str
    twilio_from: str
    phone: str

    KEYS = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "ALARM_PHONE_NUMBER",
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ChannelSettings:
        return cls(*_read_keys(env, cls.KEYS))

    def __repr__(self) -> str:
        return "ChannelSettings(<credentials>)"


@dataclass(frozen=True)
class TelegramOnlySettings:
    """DEGRADED_ALERTING: the two Telegram keys only (no SMS fallback, no calls)."""

    telegram_token: str
    telegram_chat_id: str

    KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelegramOnlySettings:
        return cls(*_read_keys(env, cls.KEYS))

    def __repr__(self) -> str:
        return "TelegramOnlySettings(<credentials>)"


class AlertingMode(Enum):
    FULL = "FULL: Telegram, SMS fallback, calls for critical alarms"
    TELEGRAM_ONLY = "DEGRADED_ALERTING: Telegram only (no SMS fallback, no phone calls)"
    LOCAL_ONLY = "DEGRADED_ALERTING: local only (nothing reaches your phone)"

    @property
    def degraded(self) -> bool:
        return self is not AlertingMode.FULL


DEGRADED_BANNER = "[DEGRADED_ALERTING] "


def utc_now() -> datetime:
    return datetime.now(UTC)


class AlarmHub:
    """Every alarm to the record and every channel line. Each line queues and retries
    on its own thread, so one slow or dead channel never holds back the others, and
    `submit` never blocks or raises.

    `channels` are the lines fed directly by `submit` (name -> line); `fed` are lines
    fed only by another line's fallback (SMS) and by channel alerts. A channel alert
    (down, back) goes to the record and every channel except the one it is about."""

    def __init__(
        self,
        record: AlarmLine,
        channels: Mapping[str, AlarmLine] | None = None,
        fed: Mapping[str, AlarmLine] | None = None,
        mode: AlertingMode = AlertingMode.LOCAL_ONLY,
        health: tuple[ChannelHealth, ...] = (),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._record = record
        self._channels = dict(channels or {})
        self._fed = dict(fed or {})
        self.mode = mode
        self._health = health
        self._clock = clock
        for watcher in health:
            watcher.watch(self.channel_alert)

    @property
    def _all(self) -> tuple[AlarmLine, ...]:
        return (self._record, *self._channels.values(), *self._fed.values())

    def submit(self, alarm: Alarm) -> None:
        self._record.submit(alarm)
        for line in self._channels.values():
            line.submit(alarm)

    def channel_alert(self, channel: str, message: str) -> None:
        """A channel went down or came back: say so everywhere else. Never raises."""
        try:
            alarm = Alarm(self._clock(), message)
            self._record.submit(alarm)
            for name, line in (*self._channels.items(), *self._fed.items()):
                if name != channel:
                    line.submit(alarm)
        except Exception:
            log.exception("could not raise a channel alert")

    @property
    def health(self) -> list[str]:
        return [watcher.status for watcher in self._health]

    @property
    def pending(self) -> int:
        return sum(line.pending for line in self._all)

    def close(self, timeout: timedelta) -> bool:
        """Every line at once, so hung channels cost one timeout, not one each."""
        lines = self._all
        results: list[bool] = []

        def close_one(line: AlarmLine) -> None:
            results.append(line.close(timeout))

        threads = [threading.Thread(target=close_one, args=(line,)) for line in lines]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2 * timeout.total_seconds() + 1)  # a line joins worker + watchdog
        return len(results) == len(lines) and all(results)


TELEGRAM, SMS, CALL = "Telegram", "SMS", "phone call"


def alarm_hub(
    record: Callable[[Alarm], None],
    settings: AlarmSettings,
    channels: ChannelSettings | TelegramOnlySettings | None,
    post: Post = http_post,
    clock: Callable[[], datetime] = utc_now,
) -> AlarmHub:
    """The record (file and terminal) always. FULL: Telegram for every alarm with SMS
    as its fallback, and a phone call for the critical ones. Telegram only: Telegram,
    every message marked DEGRADED_ALERTING. None: local only. Only the record's line
    logs each alarm (the others would repeat it)."""
    file_line = AlarmLine.from_settings(record, settings)
    if channels is None:
        return AlarmHub(file_line, mode=AlertingMode.LOCAL_ONLY, clock=clock)
    timeout = float(settings.channel_timeout_seconds)
    if isinstance(channels, TelegramOnlySettings):
        telegram = ChannelHealth(
            TELEGRAM,
            TelegramSink(
                channels.telegram_token,
                channels.telegram_chat_id,
                timeout,
                post,
                prefix=DEGRADED_BANNER,
            ),
            clock,
        )
        push = AlarmLine.from_settings(telegram, settings, announce=False)
        return AlarmHub(
            file_line,
            {TELEGRAM: push},
            mode=AlertingMode.TELEGRAM_ONLY,
            health=(telegram,),
            clock=clock,
        )
    c = channels
    sms_watch = ChannelHealth(
        SMS,
        TwilioSmsSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post),
        clock,
    )
    sms = AlarmLine.from_settings(sms_watch, settings, announce=False)
    telegram = ChannelHealth(
        TELEGRAM, TelegramSink(c.telegram_token, c.telegram_chat_id, timeout, post), clock
    )
    push = AlarmLine.from_settings(telegram, settings, fallback=sms, announce=False)
    call_watch = ChannelHealth(
        CALL,
        TwilioCallSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post),
        clock,
    )
    calls = AlarmLine.from_settings(
        CriticalCalls(call_watch, float(settings.call_cooldown_seconds)), settings, announce=False
    )
    return AlarmHub(
        file_line,
        {TELEGRAM: push, CALL: calls},
        {SMS: sms},
        mode=AlertingMode.FULL,
        health=(telegram, sms_watch, call_watch),
        clock=clock,
    )
