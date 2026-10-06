"""The phone alarms (D20): Telegram for every alarm, SMS when it fails, a call for the
critical ones; no credential ever in an error, a log line or a repr."""

from __future__ import annotations

import base64
import io
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs

import pytest

from footprint.core.config import AlarmSettings, Profiles, load_profiles
from footprint.execution.channels import (
    CRITICAL_MARKERS,
    ChannelError,
    ChannelSettings,
    CriticalCalls,
    TelegramSink,
    TwilioCallSink,
    TwilioSmsSink,
    alarm_hub,
    http_post,
    is_critical,
)
from footprint.execution.management.malik import Alarm
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
TOKEN = "123456:SECRET-TOKEN"
SID, AUTH = "AC" + "0123456789abcdef" * 2, "twilioSECRET0123"
FROM, PHONE = "+15550001111", "+15559998888"
SECRETS = (TOKEN, AUTH, PHONE)


class Post:
    """A fake http_post: records calls, answers `status` (or raises `error`)."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status, self.error = status, error
        self.calls: list[tuple[str, dict[str, str], tuple[str, str] | None, float]] = []

    def __call__(
        self, url: str, data: Mapping[str, str], auth: tuple[str, str] | None, timeout: float
    ) -> int:
        self.calls.append((url, dict(data), auth, timeout))
        if self.error is not None:
            raise self.error
        return self.status


def alarm(message: str = "something happened") -> Alarm:
    return Alarm(NOW, message)


def channels() -> ChannelSettings:
    return ChannelSettings(TOKEN, "4242", SID, AUTH, FROM, PHONE)


def env_values() -> dict[str, str]:
    """A well-formed .env for the six keys."""
    return dict(zip(ChannelSettings.KEYS, [TOKEN, "4242", SID, AUTH, FROM, PHONE], strict=True))


def no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# --- the sinks -------------------------------------------------------------------------


def test_telegram_sends_the_alarm_to_the_chat() -> None:
    post = Post()
    TelegramSink(TOKEN, "4242", 8, post)(alarm("HALT: x"))
    ((url, data, auth, timeout),) = post.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert data["chat_id"] == "4242"
    assert "HALT: x" in data["text"]
    assert data["text"].startswith("FOOTPRINT 2026-10-06 14:00:00Z")
    assert (auth, timeout) == (None, 8)


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_a_refused_telegram_send_raises_without_the_token(status: int) -> None:
    with pytest.raises(ChannelError) as failed:
        TelegramSink(TOKEN, "4242", 8, Post(status))(alarm())
    assert f"HTTP {status}" in str(failed.value)
    no_secret(str(failed.value))


def test_an_unreachable_channel_raises_without_secrets() -> None:
    for sink in (
        TelegramSink(TOKEN, "4242", 8, Post(error=ChannelError("no answer (timeout)"))),
        TwilioSmsSink(SID, AUTH, FROM, PHONE, 8, Post(error=ChannelError("no answer (x)"))),
        TwilioCallSink(SID, AUTH, FROM, PHONE, 8, Post(error=ChannelError("no answer (x)"))),
    ):
        with pytest.raises(ChannelError) as failed:
            sink(alarm())
        no_secret(str(failed.value))
        no_secret(repr(sink))
        named = {TelegramSink: "Telegram: ", TwilioSmsSink: "Twilio SMS: "}
        assert str(failed.value).startswith(named.get(type(sink), "Twilio call: "))


def test_sms_goes_from_the_twilio_number_to_andre_with_basic_auth() -> None:
    post = Post(201)
    TwilioSmsSink(SID, AUTH, FROM, PHONE, 8, post)(alarm("x" * 3000))
    ((url, data, auth, _),) = post.calls
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert (data["From"], data["To"]) == (FROM, PHONE)
    assert auth == (SID, AUTH)
    assert len(data["Body"]) == 1600  # Twilio's limit, clipped with an ellipsis
    assert data["Body"].endswith("…")


def test_a_call_reads_the_alarm_out_safely() -> None:
    post = Post(201)
    TwilioCallSink(SID, AUTH, FROM, PHONE, 8, post)(alarm("HALT: <x> & y" + "z" * 900))
    ((url, data, auth, _),) = post.calls
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert auth == (SID, AUTH)
    twiml = data["Twiml"]
    assert twiml.startswith("<Response><Say>Footprint critical alarm. HALT: &lt;x&gt; &amp; y")
    assert twiml.endswith("Check your phone.</Say></Response>")
    assert len(twiml) < 600  # clipped: a call never reads out a whole report


# --- the real HTTP post --------------------------------------------------------------------


class Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str | None, dict[str, list[str]]]]] = []
    status = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = parse_qs(self.rfile.read(length).decode())
        Recorder.seen.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(Recorder.status)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Any:
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    Recorder.seen, Recorder.status = [], 200
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def test_http_post_sends_a_form_with_basic_auth(server: Any) -> None:
    url = f"http://127.0.0.1:{server.server_port}/x"
    assert http_post(url, {"Body": "hi & bye"}, (SID, AUTH), 5) == 200
    ((path, header, body),) = Recorder.seen
    assert path == "/x"
    assert header == "Basic " + base64.b64encode(f"{SID}:{AUTH}".encode()).decode()
    assert body == {"Body": ["hi & bye"]}


def test_http_post_returns_an_error_status_instead_of_raising(server: Any) -> None:
    Recorder.status = 401
    url = f"http://127.0.0.1:{server.server_port}/x"
    assert http_post(url, {}, None, 5) == 401


@pytest.mark.parametrize("bad", [" # comment", "\tx", "\nx"])
def test_http_post_never_shows_a_malformed_url(bad: str) -> None:
    """http.client refuses a URL with a space or control character with an error that
    quotes it (InvalidURL, not an OSError): it must not escape with the token in it."""
    url = f"https://api.telegram.org/bot{TOKEN}{bad}/sendMessage"
    with pytest.raises(ChannelError) as failed:
        http_post(url, {}, None, 2)
    no_secret(str(failed.value))
    assert failed.value.__cause__ is None
    assert failed.value.__suppress_context__


def test_a_failing_channel_never_logs_a_secret(
    profiles: Profiles, caplog: pytest.LogCaptureFixture
) -> None:
    """The real http_post on a malformed URL, through the hub's retrying line."""
    broken = replace(channels(), telegram_token=TOKEN + " # x")  # (from_env refuses this)
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), broken, http_post)
    with caplog.at_level("DEBUG"):
        hub.submit(alarm("cleared to trade by Andre"))
        time.sleep(1.5)
        hub.close(timedelta(seconds=1))
    assert "alarm line down" in caplog.text
    no_secret(caplog.text)


def test_http_post_never_shows_the_url_when_nothing_answers() -> None:
    url = f"http://127.0.0.1:9/bot{TOKEN}/sendMessage"  # nothing listens on port 9
    with pytest.raises(ChannelError) as failed:
        http_post(url, {}, None, 2)
    no_secret(str(failed.value))
    assert failed.value.__cause__ is None  # the URL-bearing error is not chained


# --- what rings the phone --------------------------------------------------------------


def test_the_critical_alarms_are_the_ones_that_need_hands() -> None:
    for message in (
        "HALT: unclear recovery: broker, ledger and records disagree",
        "KILL SWITCH LATCHED: no entries until Andre clears it",
        "cannot price the SELL of 1 NQ-x: no quote seen yet. ... flatten by hand if it does not",
        "IBKR SESSION KEEPS FAILING: 5 attempts in a row",
        "p1: unprotected, flattening",
        "market data step failed (TimeoutError()): no new data until fixed",
    ):
        assert is_critical(message), message
    for message in (
        "STARTED: reconciling; no entries until Andre clears to trade",
        "cleared to trade by Andre",
        "market data is stepping again",
        "IBKR connection lost: nothing is sent until it is back and resynced",
    ):
        assert not is_critical(message), message


def test_every_critical_marker_is_still_said_somewhere_in_the_code() -> None:
    """Renaming an alarm must not silently stop its phone call."""
    source = "\n".join(
        p.read_text() for p in (REPO_ROOT / "src").rglob("*.py") if p.name != "channels.py"
    )
    for marker in CRITICAL_MARKERS:
        assert marker in source, f"no alarm says {marker!r} any more"


def test_calls_ring_only_for_critical_alarms_and_at_most_once_per_cooldown() -> None:
    rung: list[Alarm] = []
    clock = [100.0]
    calls = CriticalCalls(rung.append, 600, lambda: clock[0])
    calls(alarm("market data is stepping again"))
    assert rung == []
    calls(alarm("HALT: one"))
    calls(alarm("HALT: two"))  # within the cooldown: push and SMS only
    clock[0] += 600
    calls(alarm("HALT: three"))
    assert [a.message for a in rung] == ["HALT: one", "HALT: three"]


def test_one_kind_of_critical_alarm_never_silences_another() -> None:
    rung: list[str] = []
    calls = CriticalCalls(lambda a: rung.append(a.message), 600, lambda: 0.0)
    calls(alarm("IBKR resync is not completing (900s): nothing is sent or trusted"))
    calls(alarm("IBKR resync is not completing (1200s): nothing is sent or trusted"))
    calls(alarm("KILL SWITCH LATCHED: no entries until Andre clears it"))
    calls(alarm("p1: unprotected, flattening"))
    assert [m.split(" ")[0] for m in rung] == ["IBKR", "KILL", "p1:"]


def test_a_failed_call_is_retried_and_starts_no_cooldown() -> None:
    clock = [0.0]
    attempts: list[str] = []

    def flaky(a: Alarm) -> None:
        attempts.append(a.message)
        if len(attempts) == 1:
            raise ChannelError("Twilio call answered HTTP 503")

    calls = CriticalCalls(flaky, 600, lambda: clock[0])
    with pytest.raises(ChannelError):
        calls(alarm("HALT: x"))
    calls(alarm("HALT: x"))  # the line's retry: rings now
    assert attempts == ["HALT: x", "HALT: x"]


# --- settings ----------------------------------------------------------------------------


def test_channel_settings_need_all_six_keys_and_never_echo_a_value() -> None:
    env = dict(zip(ChannelSettings.KEYS, ["v1", "v2", "", "v4", "v5", " "], strict=True))
    with pytest.raises(ValueError, match=r"missing in \.env") as missing:
        ChannelSettings.from_env(env)
    assert "TWILIO_ACCOUNT_SID" in str(missing.value)
    assert "ALARM_PHONE_NUMBER" in str(missing.value)
    assert "v1" not in str(missing.value)
    ok = ChannelSettings.from_env({k: f" {v} " for k, v in env_values().items()})
    assert ok == channels()
    no_secret(repr(ok))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TELEGRAM_BOT_TOKEN", TOKEN + "   # from BotFather"),
        ("TELEGRAM_BOT_TOKEN", "123456:SECRET\tTOKEN"),
        ("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN"),
        ("TELEGRAM_CHAT_ID", "@andre"),
        ("TELEGRAM_CHAT_ID", "\u0664\u0662"),  # Arabic-Indic digits: ASCII only
        ("TWILIO_ACCOUNT_SID", SID + " x"),
        ("TWILIO_ACCOUNT_SID", "AC123"),
        ("TWILIO_AUTH_TOKEN", AUTH + " #"),
        ("TWILIO_FROM_NUMBER", "555-000-1111"),
        ("ALARM_PHONE_NUMBER", "15559998888"),
    ],
)
def test_a_malformed_key_is_refused_by_name_never_by_value(key: str, value: str) -> None:
    env = env_values() | {key: value}
    with pytest.raises(ValueError, match="malformed") as bad:
        ChannelSettings.from_env(env)
    assert key in str(bad.value)
    assert value.strip() not in str(bad.value)
    no_secret(str(bad.value))


def test_the_example_env_lists_every_key_empty() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    for key in ChannelSettings.KEYS:
        assert f"\n{key}=\n" in text


def test_a_channel_timeout_that_would_hold_back_the_fallback_is_refused(
    tmp_path: Path,
) -> None:
    from footprint.core.config import ConfigError, load_execution

    source = (REPO_ROOT / "config" / "execution.yaml").read_text()
    bad = tmp_path / "execution.yaml"
    bad.write_text(source.replace("channel_timeout_seconds: 8", "channel_timeout_seconds: 10"))
    with pytest.raises(ConfigError, match="channel_timeout_seconds"):
        load_execution(bad)


# --- the hub ---------------------------------------------------------------------------------


def fast(settings: AlarmSettings) -> AlarmSettings:
    return replace(settings, retry_seconds=1)


@pytest.fixture
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


def test_local_only_records_every_alarm_and_rings_nothing(profiles: Profiles) -> None:
    recorded: list[Alarm] = []
    post = Post()
    hub = alarm_hub(recorded.append, fast(profiles.execution.alarms), None, post)
    hub.submit(alarm("HALT: x"))
    assert hub.close(timedelta(seconds=2))
    assert [a.message for a in recorded] == ["HALT: x"]
    assert post.calls == []


def test_every_alarm_goes_to_the_record_and_telegram_and_critical_ones_ring(
    profiles: Profiles,
) -> None:
    recorded: list[Alarm] = []
    post = Post()
    hub = alarm_hub(recorded.append, fast(profiles.execution.alarms), channels(), post)
    hub.submit(alarm("cleared to trade by Andre"))
    hub.submit(alarm("HALT: something"))
    assert hub.close(timedelta(seconds=3))
    assert [a.message for a in recorded] == ["cleared to trade by Andre", "HALT: something"]
    telegram = [d["text"] for u, d, _, _ in post.calls if "telegram" in u]
    calls = [d["Twiml"] for u, d, _, _ in post.calls if u.endswith("Calls.json")]
    sms = [u for u, _, _, _ in post.calls if u.endswith("Messages.json")]
    assert len(telegram) == 2
    assert len(calls) == 1
    assert "HALT: something" in calls[0]
    assert sms == []  # Telegram worked: no SMS


def test_when_telegram_fails_the_alarm_goes_by_sms(profiles: Profiles) -> None:
    class TelegramDown(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            return 502 if "telegram" in url else 201

    post = TelegramDown()
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), post)
    hub.submit(alarm("cleared to trade by Andre"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
        u.endswith("Messages.json") for u, *_ in post.calls
    ):
        time.sleep(0.05)
    hub.close(timedelta(seconds=1))
    sms = [d["Body"] for u, d, _, _ in post.calls if u.endswith("Messages.json")]
    assert sms
    assert "cleared to trade by Andre" in sms[0]


def test_the_sms_fallback_is_waited_for_and_counted_on_close(profiles: Profiles) -> None:
    """Telegram fails once (the alarm goes to SMS), then recovers; the SMS still in
    flight is counted as waiting and makes close report it undelivered."""
    release = threading.Event()

    class Flaky(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            if "telegram" in url:
                return 502 if sum("telegram" in u for u, *_ in self.calls) == 1 else 200
            release.wait(10)
            return 201

    post = Flaky()
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), post)
    hub.submit(alarm("cleared to trade by Andre"))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not (
        any(u.endswith("Messages.json") for u, *_ in post.calls)
        and sum("telegram" in u for u, *_ in post.calls) >= 2
    ):
        time.sleep(0.05)
    time.sleep(0.1)  # Telegram's retry finishes; only the SMS is left
    try:
        assert hub.pending == 1
        assert not hub.close(timedelta(seconds=0.3))
    finally:
        release.set()


def test_a_hung_telegram_hands_the_alarm_to_sms(profiles: Profiles) -> None:
    release = threading.Event()

    class TelegramHangs(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            if "telegram" in url:
                release.wait(10)  # never answers (until the test ends)
            return 201

    post = TelegramHangs()
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), post)
    try:
        hub.submit(alarm("cleared to trade by Andre"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            u.endswith("Messages.json") for u, *_ in post.calls
        ):
            time.sleep(0.05)
        sms = [d["Body"] for u, d, _, _ in post.calls if u.endswith("Messages.json")]
        assert sms
        assert "cleared to trade by Andre" in sms[0]
    finally:
        release.set()
        hub.close(timedelta(seconds=1))


def test_each_alarm_is_logged_once(profiles: Profiles, caplog: pytest.LogCaptureFixture) -> None:
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), Post())
    with caplog.at_level("CRITICAL"):
        hub.submit(alarm("HALT: logged once"))
        assert hub.close(timedelta(seconds=2))
    assert caplog.text.count("HALT: logged once") == 1


def test_closing_hung_channels_costs_one_timeout_not_one_each(profiles: Profiles) -> None:
    release = threading.Event()

    class AllHang(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            release.wait(10)
            return 200

    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), AllHang())
    try:
        hub.submit(alarm("HALT: everything hangs"))
        time.sleep(1.3)  # SMS gets it too, and hangs
        started = time.monotonic()
        assert not hub.close(timedelta(seconds=0.5))
        assert time.monotonic() - started < 1.5  # three hung lines in parallel
    finally:
        release.set()


def test_the_live_program_sends_its_alarms_through_the_channels(
    profiles: Profiles, tmp_path: Path
) -> None:
    from footprint.live.app import build
    from tests.brokers.test_ibkr_binding import Recording, settings

    post = Post()
    live = build(
        profiles,
        settings(),
        tmp_path / "state",
        lambda a: None,
        lambda now: (),
        channels=channels(),
        post=post,
        new_ledger=True,
        client_factory=Recording,
        clock=lambda: NOW,
    )
    live.alarm("HALT: test")
    assert live.alarms.close(timedelta(seconds=3))
    assert any("STARTED: reconciling" in d.get("text", "") for _, d, _, _ in post.calls)
    assert any(u.endswith("Calls.json") for u, *_ in post.calls)


def test_the_program_refuses_to_start_without_alarm_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from footprint.live.__main__ import main

    env = tmp_path / ".env"
    env.write_text(
        "BROKER_HOST=127.0.0.1\nBROKER_PORT=4002\nBROKER_ACCOUNT_ID=DU1\nBROKER_CLIENT_ID=0\n"
        "TELEGRAM_BOT_TOKEN=abc\n"
    )
    code = main(["--env", str(env), "--no-blackouts", "--state", str(tmp_path / "s")])
    assert code == 2
    err = capsys.readouterr().err
    assert "TELEGRAM_CHAT_ID" in err
    assert "--alarms-local-only" in err
    assert "abc" not in err


# --- the setup tool -----------------------------------------------------------------------------


def test_chat_ids_lists_who_has_messaged_the_bot() -> None:
    from footprint.live.alarm_setup import chat_ids

    seen: list[str] = []

    def fetch(url: str, timeout: float) -> Any:
        seen.append(url)
        return {
            "ok": True,
            "result": [
                {"message": {"chat": {"id": 4242, "first_name": "Andre"}}},
                {"message": {"chat": {"id": 4242, "first_name": "Andre"}}},
                {"edited_message": {}},
            ],
        }

    assert chat_ids(TOKEN, 8, fetch) == [("4242", "Andre")]
    assert seen == [f"https://api.telegram.org/bot{TOKEN}/getUpdates"]


def test_the_test_send_reports_each_channel_without_secrets() -> None:
    from footprint.live.alarm_setup import send_tests

    class SmsDown(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            return 401 if url.endswith("Messages.json") else 200

    results = send_tests(channels(), 8, SmsDown())
    assert results[0] == ("Telegram", "OK")
    assert results[1] == ("SMS", "FAILED: Twilio SMS answered HTTP 401")
    assert results[2][0] == "call"
    assert results[2][1].startswith("ACCEPTED by Twilio (not proof of delivery)")
    no_secret(str(results))


def test_the_setup_tool_needs_the_token(tmp_path: Path) -> None:
    from footprint.live.alarm_setup import main

    env = tmp_path / ".env"
    env.write_text("BROKER_HOST=x\n")
    out = io.StringIO()
    assert main(["chat-id", "--env", str(env), "--config", str(REPO_ROOT / "config")], out) == 2
    assert "TELEGRAM_BOT_TOKEN is missing" in out.getvalue()


def test_the_setup_test_fails_when_any_channel_fails(tmp_path: Path) -> None:
    from footprint.live.alarm_setup import main

    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in env_values().items()))
    config = ["--env", str(env), "--config", str(REPO_ROOT / "config")]
    out = io.StringIO()
    assert main(["test", *config], out, post=Post(200)) == 0
    assert "Telegram: OK" in out.getvalue()
    assert out.getvalue().count("ACCEPTED by Twilio") == 2  # SMS and call: check the phone
    out = io.StringIO()
    assert main(["test", *config], out, post=Post(401)) == 1
    assert "FAILED" in out.getvalue()
    no_secret(out.getvalue())


def test_the_setup_chat_id_prints_the_line_for_env(tmp_path: Path) -> None:
    from footprint.live.alarm_setup import main

    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
    config = ["chat-id", "--env", str(env), "--config", str(REPO_ROOT / "config")]
    found = {"result": [{"message": {"chat": {"id": 7, "username": "andre"}}}]}
    out = io.StringIO()
    assert main(config, out, fetch=lambda url, timeout: found) == 0
    assert "TELEGRAM_CHAT_ID=7" in out.getvalue()
    no_secret(out.getvalue())
    out = io.StringIO()
    assert main(config, out, fetch=lambda url, timeout: {"result": []}) == 1
    assert "send your bot any message" in out.getvalue()


def test_the_setup_tool_never_shows_the_token_in_a_malformed_url() -> None:
    from footprint.live.alarm_setup import chat_ids

    with pytest.raises(ChannelError) as failed:
        chat_ids(TOKEN + " # from BotFather", 2)  # the real GET
    no_secret(str(failed.value))
    assert failed.value.__cause__ is None


def test_an_sms_fallback_does_not_log_the_alarm_again(
    profiles: Profiles, caplog: pytest.LogCaptureFixture
) -> None:
    class TelegramDown(Post):
        def __call__(self, url: str, *args: Any) -> int:
            super().__call__(url, *args)
            return 502 if "telegram" in url else 201

    post = TelegramDown()
    hub = alarm_hub(lambda a: None, fast(profiles.execution.alarms), channels(), post)
    with caplog.at_level("CRITICAL"):
        hub.submit(alarm("cleared to trade by Andre"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            u.endswith("Messages.json") for u, *_ in post.calls
        ):
            time.sleep(0.05)
        hub.close(timedelta(seconds=1))
    assert any(u.endswith("Messages.json") for u, *_ in post.calls)
    said = [r for r in caplog.records if r.getMessage() == "cleared to trade by Andre"]
    assert len(said) == 1  # (Telegram closing with it undelivered says so, separately)


def test_a_line_that_will_not_close_in_time_is_reported() -> None:
    from footprint.execution.channels import AlarmHub

    class Stuck:
        pending = 0

        def submit(self, alarm: Alarm) -> None:
            pass

        def close(self, timeout: timedelta) -> bool:
            time.sleep(1.5)
            return True

    hub = AlarmHub([Stuck()])  # type: ignore[list-item]
    assert not hub.close(timedelta(seconds=0.1))
