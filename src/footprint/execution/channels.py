"""Where alarms reach Andre (decision D20): Telegram push for every alarm, SMS when
Telegram fails, and a phone call for the critical few.

Each sink is a callable for an AlarmLine (execution/alarms.py): it delivers one alarm
or raises, and the line retries (never blocking the trading loop). A sink raises
`ChannelError` with a message that never contains a credential: a token in an error,
a log or an alarm would leak it.

    TelegramSink   POST api.telegram.org/bot<token>/sendMessage
    TwilioSmsSink  POST api.twilio.com/2010-04-01/Accounts/<sid>/Messages.json
    TwilioCallSink POST api.twilio.com/2010-04-01/Accounts/<sid>/Calls.json (spoken)

`is_critical` decides which alarms also ring the phone: a halt, the kill switch, a
position or order that needs hands, IBKR refusing or failing for good, an expired
exchange calendar. `CriticalCalls` calls at most once per cooldown; the alarm still
goes out by push and SMS every time.
"""

from __future__ import annotations

import base64
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
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
    "IBKR CONNECTION REFUSED",
    "IBKR SESSION KEEPS FAILING",
    "resync is not completing",
    "EXCHANGE CALENDAR EXPIRED",
)

_TELEGRAM = "https://api.telegram.org"
_TWILIO = "https://api.twilio.com/2010-04-01/Accounts"
_TELEGRAM_MAX = 4096  # Telegram's limit on one message's text
_SMS_MAX = 1600  # Twilio's limit on one message body
_SPOKEN_MAX = 400  # a call reads out at most this much, then says to check the phone

Post = Callable[[str, Mapping[str, str], tuple[str, str] | None, float], int]


class ChannelError(RuntimeError):
    """A delivery failed. The message names the channel and the HTTP status only."""


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
    except (urllib.error.URLError, OSError) as exc:
        reason = type(exc.reason if isinstance(exc, urllib.error.URLError) else exc).__name__
        raise ChannelError(f"no answer ({reason})") from None


def is_critical(message: str) -> bool:
    return any(marker in message for marker in CRITICAL_MARKERS)


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

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TELEGRAM}/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": _clip(_text(alarm), _TELEGRAM_MAX)}
        try:
            status = self.post(url, data, None, self.timeout)
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

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TWILIO}/{self.account_sid}/Messages.json"
        data = {
            "From": self.from_number,
            "To": self.to_number,
            "Body": _clip(_text(alarm), _SMS_MAX),
        }
        try:
            status = self.post(url, data, (self.account_sid, self.auth_token), self.timeout)
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

    def __call__(self, alarm: Alarm) -> None:
        url = f"{_TWILIO}/{self.account_sid}/Calls.json"
        spoken = _clip(alarm.message, _SPOKEN_MAX)
        twiml = (
            "<Response><Say>Footprint critical alarm. "
            f"{escape(spoken)}. Check your phone.</Say></Response>"
        )
        data = {"From": self.from_number, "To": self.to_number, "Twiml": twiml}
        try:
            status = self.post(url, data, (self.account_sid, self.auth_token), self.timeout)
        except ChannelError as exc:
            raise ChannelError(f"Twilio call: {exc}") from None
        _ok(status, "Twilio call")

    def __repr__(self) -> str:
        return "TwilioCallSink(<credentials>)"


class CriticalCalls:
    """A sink that rings only for critical alarms, at most once per `cooldown` seconds
    (a repeating alarm still reaches Telegram and SMS each time). A call that fails
    raises, so the alarm line retries it."""

    def __init__(
        self,
        call: Callable[[Alarm], None],
        cooldown: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._call = call
        self._cooldown = cooldown
        self._monotonic = monotonic
        self._last: float | None = None

    def __call__(self, alarm: Alarm) -> None:
        if not is_critical(alarm.message):
            return
        now = self._monotonic()
        if self._last is not None and now - self._last < self._cooldown:
            log.info("critical alarm not rung (a call was made %.0fs ago)", now - self._last)
            return
        self._call(alarm)  # raises on failure: retried, and the cooldown not started
        self._last = now


@dataclass(frozen=True)
class ChannelSettings:
    """From .env (credentials) and config (timing)."""

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
        """All six keys, or ValueError naming the missing ones (never a value)."""
        missing = [k for k in cls.KEYS if not env.get(k, "").strip()]
        if missing:
            raise ValueError(f"missing in .env: {', '.join(missing)}")
        values = [env[k].strip() for k in cls.KEYS]
        return cls(*values)

    def __repr__(self) -> str:
        return "ChannelSettings(<credentials>)"


class AlarmHub:
    """Every alarm to every line (the file record, the phone lines). Each line queues
    and retries on its own thread, so one slow or dead channel never holds back the
    others, and `submit` never blocks or raises."""

    def __init__(self, lines: Sequence[AlarmLine], also_close: Sequence[AlarmLine] = ()) -> None:
        self._lines = tuple(lines)
        self._all = (*self._lines, *also_close)  # fallback lines: fed by their primary

    def submit(self, alarm: Alarm) -> None:
        for line in self._lines:
            line.submit(alarm)

    @property
    def pending(self) -> int:
        return sum(line.pending for line in self._all)

    def close(self, timeout: timedelta) -> bool:
        return all([line.close(timeout) for line in self._all])


def alarm_hub(
    record: Callable[[Alarm], None],
    settings: AlarmSettings,
    channels: ChannelSettings | None,
    post: Post = http_post,
) -> AlarmHub:
    """The record (file and terminal) always; with `channels`, Telegram for every alarm
    with SMS as its fallback, and a phone call for the critical ones."""
    file_line = AlarmLine.from_settings(record, settings)
    if channels is None:
        return AlarmHub([file_line])
    timeout = float(settings.channel_timeout_seconds)
    c = channels
    sms = AlarmLine.from_settings(
        TwilioSmsSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post),
        settings,
    )
    push = AlarmLine.from_settings(
        TelegramSink(c.telegram_token, c.telegram_chat_id, timeout, post), settings, fallback=sms
    )
    call = TwilioCallSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post)
    calls = AlarmLine.from_settings(
        CriticalCalls(call, float(settings.call_cooldown_seconds)), settings
    )
    return AlarmHub([file_line, push, calls], also_close=[sms])
