"""Remediation items 3 and 10: a failing or hanging alarm channel never blocks later
alarms and is reported on the other channels; DEGRADED_ALERTING (Telegram only, local
only) is explicit, always labelled, and blocks unattended trading."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from footprint.core.config import AlarmSettings, Profiles, load_profiles
from footprint.execution.channels import (
    DEGRADED_BANNER,
    AlertingMode,
    ChannelError,
    TelegramOnlySettings,
    alarm_hub,
    within,
)
from footprint.execution.management.malik import Alarm
from tests.conftest import REPO_ROOT
from tests.live.test_channels import NOW, SECRETS, TOKEN, alarm, channels, env_values, no_secret

CALLS, SMS, TELEGRAM = "Calls.json", "Messages.json", "telegram"


@pytest.fixture
def profiles() -> Profiles:
    return load_profiles(REPO_ROOT / "config", "nq.yaml")


def quick(settings: AlarmSettings) -> AlarmSettings:
    return replace(settings, retry_seconds=1, channel_timeout_seconds=1)


class Net:
    """A fake http_post that answers per channel; `hang` makes a channel never answer
    (until released). Records (channel, text) for every request."""

    def __init__(self, **answers: int) -> None:
        self.answers = {CALLS: 201, SMS: 201, TELEGRAM: 200, **answers}
        self.hang: set[str] = set()
        self.release = threading.Event()
        self.sent: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def channel(self, url: str) -> str:
        return next(c for c in (CALLS, SMS, TELEGRAM) if c in url)

    def __call__(self, url: str, data: Mapping[str, str], auth: Any, timeout: float) -> int:
        channel = self.channel(url)
        text = data.get("text") or data.get("Body") or data.get("Twiml") or ""
        with self._lock:
            self.sent.append((channel, text))
        if channel in self.hang:
            self.release.wait(10)
        return self.answers[channel]

    def texts(self, channel: str) -> list[str]:
        with self._lock:
            return [t for c, t in self.sent if c == channel]


def wait_for(condition: Callable[[], bool], seconds: float = 6) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# --- the hard deadline -----------------------------------------------------------------------


def test_a_send_that_hangs_is_abandoned_at_its_deadline() -> None:
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(ChannelError, match="no answer within"):
        within(0.2, lambda: release.wait(10))
    assert time.monotonic() - started < 1
    release.set()


def test_an_unexpected_error_becomes_a_channel_error_naming_only_its_type() -> None:
    def boom() -> int:
        raise ValueError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")

    with pytest.raises(ChannelError) as failed:
        within(1, boom)
    assert "ValueError" in str(failed.value)
    no_secret(str(failed.value))
    assert failed.value.__cause__ is None


# --- a failing phone-call channel (Aegis M2) ------------------------------------------------------


def test_a_hanging_call_never_blocks_the_next_critical_alarm(profiles: Profiles) -> None:
    """Twilio hangs: the first call is cut at its deadline; the next critical alarm is
    not held behind it (Telegram carries it at once) and its call fails at once while
    the hung request is still out (one abandoned send per channel at most). Once the
    hung request ends, calls are tried again."""
    net = Net()
    net.hang.add(CALLS)
    recorded: list[Alarm] = []
    hub = alarm_hub(recorded.append, quick(profiles.execution.alarms), channels(), net)
    try:
        hub.submit(alarm("HALT: step failed: X"))
        hub.submit(alarm("KILL SWITCH LATCHED at drawdown"))
        assert wait_for(lambda: any("KILL SWITCH" in t for t in net.texts(TELEGRAM)), 3)
        assert wait_for(lambda: any("CHANNEL DOWN: phone call" in t for t in net.texts(SMS)))
        assert len(net.texts(CALLS)) == 1  # no second thread piled onto a hung Twilio
        net.release.set()
        net.hang.clear()
        assert wait_for(  # the hung request has really ended
            lambda: not any(t.name == "alarm-send" for t in threading.enumerate()), 3
        )
        hub.submit(alarm("CANCEL STUCK since 10:00"))
        assert wait_for(lambda: any("CANCEL STUCK" in t for t in net.texts(CALLS)), 5)
    finally:
        net.release.set()
        hub.close(timedelta(seconds=1))


def test_a_rejected_call_is_reported_by_telegram_sms_and_the_record(profiles: Profiles) -> None:
    net = Net(**{CALLS: 400})
    recorded: list[Alarm] = []
    hub = alarm_hub(recorded.append, quick(profiles.execution.alarms), channels(), net)
    hub.submit(alarm("HALT: step failed: X"))
    hub.submit(alarm("KILL SWITCH LATCHED at drawdown"))
    assert wait_for(lambda: any("ALARM CHANNEL DOWN: phone call" in t for t in net.texts(SMS)))
    assert wait_for(lambda: len(net.texts(CALLS)) >= 2)
    hub.close(timedelta(seconds=2))
    assert len(net.texts(CALLS)) == 2  # one try per alarm: nothing retried in a loop
    assert any("ALARM CHANNEL DOWN: phone call" in t for t in net.texts(TELEGRAM))
    assert any("ALARM CHANNEL DOWN: phone call" in a.message for a in recorded)  # alarms.log
    assert any("HTTP 400" in a.message for a in recorded)
    assert any("phone call: DOWN" in h for h in hub.health)
    for _, text in net.sent:
        no_secret(text)


def test_repeated_call_failures_report_the_channel_down_once(profiles: Profiles) -> None:
    net = Net(**{CALLS: 503})
    recorded: list[Alarm] = []
    hub = alarm_hub(recorded.append, quick(profiles.execution.alarms), channels(), net)
    for kind in ("HALT: a", "KILL SWITCH LATCHED", "CANCEL STUCK since", "flatten by hand"):
        hub.submit(alarm(kind))
    assert wait_for(lambda: len(net.texts(CALLS)) >= 4)
    hub.close(timedelta(seconds=2))
    downs = [a for a in recorded if "ALARM CHANNEL DOWN" in a.message]
    assert len(downs) == 1
    assert "(4 failed)" in " ".join(hub.health)


def test_a_call_channel_that_recovers_says_so(profiles: Profiles) -> None:
    net = Net(**{CALLS: 503})
    recorded: list[Alarm] = []
    hub = alarm_hub(recorded.append, quick(profiles.execution.alarms), channels(), net)
    hub.submit(alarm("HALT: first"))
    assert wait_for(lambda: any("CHANNEL DOWN" in a.message for a in recorded))
    net.answers[CALLS] = 201
    hub.submit(alarm("HALT: second"))  # same kind: no cooldown was started, so it rings
    assert wait_for(lambda: any("channel back: phone call" in a.message for a in recorded))
    assert wait_for(lambda: any("channel back: phone call" in t for t in net.texts(TELEGRAM)))
    hub.close(timedelta(seconds=2))
    assert "phone call: ok" in hub.health


def test_a_down_call_channel_is_never_rung_about_itself(profiles: Profiles) -> None:
    net = Net(**{CALLS: 400})
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), channels(), net)
    hub.submit(alarm("HALT: x"))
    assert wait_for(lambda: any("CHANNEL DOWN" in t for t in net.texts(TELEGRAM)))
    time.sleep(0.3)
    hub.close(timedelta(seconds=2))
    assert not any("CHANNEL DOWN" in t for t in net.texts(CALLS))


def test_telegram_down_is_reported_by_sms_and_a_call(profiles: Profiles) -> None:
    net = Net(**{TELEGRAM: 502})
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), channels(), net)
    hub.submit(alarm("cleared to trade by Andre"))
    assert wait_for(lambda: any("ALARM CHANNEL DOWN: Telegram" in t for t in net.texts(SMS)))
    assert wait_for(lambda: any("CHANNEL DOWN" in t for t in net.texts(CALLS)))
    assert wait_for(lambda: any("cleared to trade" in t for t in net.texts(SMS)))  # fallback
    hub.close(timedelta(seconds=2))


def test_each_channel_down_rings_on_its_own(profiles: Profiles) -> None:
    """Telegram down rang; SMS down a moment later must ring too (its own kind)."""
    from footprint.execution.channels import critical_kind

    assert critical_kind("ALARM CHANNEL DOWN: SMS failed (x)") != critical_kind(
        "ALARM CHANNEL DOWN: Telegram failed (x)"
    )


def test_sms_down_is_reported_by_telegram(profiles: Profiles) -> None:
    net = Net(**{TELEGRAM: 502, SMS: 401})
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), channels(), net)
    hub.submit(alarm("cleared to trade by Andre"))
    assert wait_for(lambda: any("ALARM CHANNEL DOWN: SMS" in t for t in net.texts(CALLS)))
    hub.close(timedelta(seconds=2))


# --- modes -----------------------------------------------------------------------------------


def test_full_alerting_is_full_and_unmarked(profiles: Profiles) -> None:
    net = Net()
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), channels(), net)
    assert hub.mode is AlertingMode.FULL
    assert not hub.mode.degraded
    hub.submit(alarm("cleared to trade by Andre"))
    assert hub.close(timedelta(seconds=2))
    assert not any(t.startswith(DEGRADED_BANNER) for t in net.texts(TELEGRAM))


def test_telegram_only_is_degraded_and_says_so_on_every_message(profiles: Profiles) -> None:
    net = Net()
    settings = TelegramOnlySettings(TOKEN, "4242")
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), settings, net)
    assert hub.mode is AlertingMode.TELEGRAM_ONLY
    assert hub.mode.degraded
    assert "DEGRADED_ALERTING" in hub.mode.value
    assert "FULL" not in hub.mode.value
    hub.submit(alarm("HALT: x"))
    hub.submit(alarm("cleared to trade by Andre"))
    assert hub.close(timedelta(seconds=2))
    assert len(net.texts(TELEGRAM)) == 2
    assert all(t.startswith(DEGRADED_BANNER) for t in net.texts(TELEGRAM))
    assert net.texts(SMS) == []  # no Twilio at all
    assert net.texts(CALLS) == []


def test_local_only_is_degraded(profiles: Profiles) -> None:
    hub = alarm_hub(lambda a: None, quick(profiles.execution.alarms), None, Net())
    assert hub.mode is AlertingMode.LOCAL_ONLY
    assert hub.mode.degraded
    assert hub.close(timedelta(seconds=1))


def test_telegram_only_needs_just_the_two_telegram_keys() -> None:
    values = env_values()
    two = {k: values[k] for k in TelegramOnlySettings.KEYS}
    assert TelegramOnlySettings.from_env(two) == TelegramOnlySettings(TOKEN, "4242")
    with pytest.raises(ValueError, match="malformed"):
        TelegramOnlySettings.from_env(two | {"TELEGRAM_BOT_TOKEN": TOKEN + " #x"})
    with pytest.raises(ValueError, match="missing"):
        TelegramOnlySettings.from_env({"TELEGRAM_CHAT_ID": "1"})
    assert TOKEN not in repr(TelegramOnlySettings(TOKEN, "4242"))


# --- unattended operation is blocked while degraded --------------------------------------------


def degraded_live(profiles: Profiles, tmp_path: Path, mode: str) -> Any:
    from footprint.live.app import Driver
    from tests.live.test_live_app import make

    net = Net()
    chosen: Any = TelegramOnlySettings(TOKEN, "4242") if mode == "telegram" else None
    if mode == "full":
        chosen = channels()
    live, sink = make(profiles, tmp_path, channels=chosen, post=net)
    out = io.StringIO()
    return live, Driver(live, out=out), out, sink


@pytest.mark.parametrize("mode", ["telegram", "local"])
def test_degraded_entries_stop_when_nobody_is_at_the_terminal(
    profiles: Profiles, tmp_path: Path, mode: str
) -> None:
    live, driver, out, _ = degraded_live(profiles, tmp_path, mode)
    window = timedelta(minutes=profiles.execution.alarms.degraded_attendance_minutes)
    driver.command("clear Andre", threading.Event())
    assert live.runner.cleared_by == "Andre"
    driver.tick(NOW + window * 0.5)
    assert live.runner.cleared_by == "Andre"
    driver.tick(NOW + window * 0.9)
    assert "type 'here'" in out.getvalue()  # warned before it stops
    driver.tick(NOW + window + timedelta(seconds=1))
    assert live.runner.cleared_by is None  # entries stopped
    live.alarms.close(timedelta(seconds=2))
    status = driver.status()
    assert "DEGRADED_ALERTING" in status
    assert "cleared to trade: no" in status


def test_any_input_keeps_a_degraded_run_attended(profiles: Profiles, tmp_path: Path) -> None:
    live, driver, _, _ = degraded_live(profiles, tmp_path, "telegram")
    window = timedelta(minutes=profiles.execution.alarms.degraded_attendance_minutes)
    clock = [NOW]
    live.clock = lambda: clock[0]
    driver.command("clear Andre", threading.Event())
    for k in range(1, 5):
        clock[0] = NOW + window * 0.7 * k
        driver.command("here", threading.Event())  # someone is there
        driver.tick(clock[0])
        assert live.runner.cleared_by == "Andre"
    live.alarms.close(timedelta(seconds=1))


def test_the_unattended_alarm_is_critical_and_recorded(profiles: Profiles, tmp_path: Path) -> None:
    from footprint.execution.channels import is_critical

    live, driver, _, sink = degraded_live(profiles, tmp_path, "local")
    window = timedelta(minutes=profiles.execution.alarms.degraded_attendance_minutes)
    driver.command("clear Andre", threading.Event())
    driver.tick(NOW + window * 2)
    live.alarms.close(timedelta(seconds=2))
    unattended = [a.message for a in sink.got if "UNATTENDED" in a.message]
    assert unattended
    assert is_critical(unattended[0])
    assert "DEGRADED_ALERTING" in unattended[0]


def test_full_alerting_never_stops_entries_for_quiet(profiles: Profiles, tmp_path: Path) -> None:
    live, driver, _, _ = degraded_live(profiles, tmp_path, "full")
    driver.command("clear Andre", threading.Event())
    driver.tick(NOW + timedelta(hours=6))
    assert live.runner.cleared_by == "Andre"
    assert "FULL" in driver.status()
    live.alarms.close(timedelta(seconds=2))


@pytest.mark.parametrize("flag", ["--alarms-telegram-only", "--alarms-local-only"])
def test_a_degraded_start_without_a_terminal_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    from footprint.live.__main__ import main
    from tests.brokers.test_ibkr_binding import ACCOUNT

    env = tmp_path / ".env"
    env.write_text(
        f"BROKER_HOST=127.0.0.1\nBROKER_PORT=4002\nBROKER_ACCOUNT_ID={ACCOUNT}\n"
        f"BROKER_CLIENT_ID=0\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=4242\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    code = main(["--env", str(env), "--no-blackouts", "--state", str(tmp_path / "s"), flag])
    assert code == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "DEGRADED_ALERTING" in err
    assert not (tmp_path / "s").exists()
    for secret in SECRETS:
        assert secret not in err


def test_the_two_degraded_flags_cannot_be_combined(tmp_path: Path) -> None:
    from footprint.live.__main__ import main

    with pytest.raises(SystemExit):
        main(["--no-blackouts", "--alarms-telegram-only", "--alarms-local-only"])


# --- the manual delivery tool (never run by the suite against the real services) --------------


def _env(tmp_path: Path, keys: dict[str, str]) -> list[str]:
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    return ["--env", str(env), "--config", str(REPO_ROOT / "config")]


def test_the_setup_tool_tests_telegram_only_and_says_it_is_degraded(tmp_path: Path) -> None:
    from footprint.live.alarm_setup import main

    values = env_values()
    args = _env(tmp_path, {k: values[k] for k in TelegramOnlySettings.KEYS})
    net, out = Net(), io.StringIO()
    assert main(["test", "--telegram-only", *args], out, post=net) == 0
    assert "Telegram: OK" in out.getvalue()
    assert "NOT full alarm coverage" in out.getvalue()
    assert [c for c, _ in net.sent] == [TELEGRAM]
    assert net.texts(TELEGRAM)[0].startswith(DEGRADED_BANNER)
    no_secret(out.getvalue())


def test_the_fallback_check_sends_nothing_to_telegram_and_needs_sms_and_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from footprint.live import alarm_setup

    real_sleep = time.sleep
    monkeypatch.setattr("footprint.live.alarm_setup.time.sleep", lambda s: real_sleep(min(s, 3)))
    config = tmp_path / "config"
    for f in (REPO_ROOT / "config").rglob("*.yaml"):
        target = config / f.relative_to(REPO_ROOT / "config")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.read_text())
    execution = config / "execution.yaml"
    execution.write_text(
        execution.read_text()
        .replace("retry_seconds: 5 ", "retry_seconds: 1 ")
        .replace("channel_timeout_seconds: 8 ", "channel_timeout_seconds: 1 ")
    )
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in env_values().items()))
    net, out = Net(), io.StringIO()
    code = alarm_setup.main(
        ["test-fallback", "--env", str(env), "--config", str(config)], out, post=net
    )
    assert net.texts(TELEGRAM) == []  # simulated down: never sent
    assert any("TEST: fallback check" in t for t in net.texts(SMS))
    assert any("ALARM CHANNEL DOWN: Telegram" in t for t in net.texts(SMS))
    assert any("CHANNEL DOWN" in t for t in net.texts(CALLS))
    assert code == 0
    assert "check the phone" in out.getvalue()
    no_secret(out.getvalue())
    net_rejects = Net(**{SMS: 401, CALLS: 401})
    assert (
        alarm_setup.main(
            ["test-fallback", "--env", str(env), "--config", str(config)],
            io.StringIO(),
            post=net_rejects,
        )
        == 1
    )


# --- AEGIS re-audit L-a and L-b ----------------------------------------------------------------


def test_a_hanging_telegram_send_is_cut_at_its_deadline() -> None:
    from footprint.execution.channels import TelegramSink

    release = threading.Event()

    def hangs(url: str, data: Mapping[str, str], auth: Any, timeout: float) -> int:
        release.wait(10)
        return 200

    sink = TelegramSink(TOKEN, "4242", 0.2, hangs)
    started = time.monotonic()
    with pytest.raises(ChannelError, match="Telegram: no answer within") as failed:
        sink(alarm("x"))
    assert time.monotonic() - started < 1
    no_secret(str(failed.value))
    release.set()


def test_a_dead_channel_never_holds_more_than_one_abandoned_send() -> None:
    from footprint.execution.channels import TwilioCallSink

    release = threading.Event()
    started: list[float] = []

    def hangs(url: str, data: Mapping[str, str], auth: Any, timeout: float) -> int:
        started.append(time.monotonic())
        release.wait(10)
        return 201

    from tests.live.test_channels import AUTH, FROM, PHONE, SID

    def sends() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "alarm-send")

    sink = TwilioCallSink(SID, AUTH, FROM, PHONE, 0.3, hangs)
    before = sends()
    for _ in range(20):
        with pytest.raises(ChannelError):
            sink(alarm("HALT: x"))
    assert wait_for(lambda: len(started) >= 1, 3)  # (slow machines: the thread starts late)
    assert len(started) == 1, started  # one send started; the other 19 failed at once
    assert sends() - before <= 1
    release.set()
    assert wait_for(lambda: not sink._busy.is_set(), 3)  # its own hung send has ended
    sink(alarm("HALT: y"))  # the channel works again once the hung send ends
    assert len(started) == 2
