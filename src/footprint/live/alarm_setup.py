"""Set up and check the phone alarms (D20).

    uv run python -m footprint.live.alarm_setup chat-id        # after messaging your bot once
    uv run python -m footprint.live.alarm_setup test           # one test alarm per channel
    uv run python -m footprint.live.alarm_setup test --telegram-only   # DEGRADED_ALERTING
    uv run python -m footprint.live.alarm_setup test-fallback  # Telegram made to fail here:
                                                               #   SMS and a call must arrive

These are the manual, real deliveries (remediation items 4 and 23): the automated tests
never reach Telegram or Twilio. Reads the keys from .env. Never prints a token, a
password or a phone number.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

from footprint.brokers.ibkr.binding import read_env_file
from footprint.core.config import AlarmSettings, load_profiles
from footprint.execution.channels import (
    DEGRADED_BANNER,
    AlertingMode,
    ChannelError,
    ChannelSettings,
    Post,
    TelegramOnlySettings,
    TelegramSink,
    TwilioCallSink,
    TwilioSmsSink,
    alarm_hub,
    http_post,
)
from footprint.execution.management.malik import Alarm

Fetch = Callable[[str, float], Any]
ACCEPTED = "ACCEPTED by Twilio (not proof of delivery)"
_TELEGRAM_URL = "https://api.telegram.org/"
_FAILED = "FAILED"


def http_get_json(url: str, timeout: float) -> Any:
    """GET and parse JSON; errors never show the URL (it holds the token)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ChannelError(f"Telegram answered HTTP {exc.code}") from None
    except Exception as exc:  # anything: some (http.client.InvalidURL) quote the URL
        raise ChannelError(f"Telegram: no usable answer ({type(exc).__name__})") from None


def chat_ids(token: str, timeout: float, fetch: Fetch = http_get_json) -> list[tuple[str, str]]:
    """(chat id, name) of everyone who has messaged the bot recently."""
    answer = fetch(f"https://api.telegram.org/bot{token}/getUpdates", timeout)
    found: dict[str, str] = {}
    for update in answer.get("result", []) if isinstance(answer, dict) else []:
        chat = (update.get("message") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("username") or chat.get("first_name") or "?"
            found[str(chat["id"])] = str(name)
    return sorted(found.items())


def send_tests(
    channels: ChannelSettings, timeout: float, post: Post = http_post
) -> list[tuple[str, str]]:
    """One test alarm per channel; (channel, result) for each. Twilio accepting an SMS
    or a call is not delivery (an unregistered number's SMS is accepted, then blocked
    by the carrier), so those say to check the phone."""
    alarm = Alarm(datetime.now(UTC), "TEST: Footprint alarm channel check. No action needed")
    c = channels
    sms = TwilioSmsSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post)
    call = TwilioCallSink(c.twilio_sid, c.twilio_token, c.twilio_from, c.phone, timeout, post)
    sinks: list[tuple[str, Callable[[Alarm], None], str]] = [
        ("Telegram", TelegramSink(c.telegram_token, c.telegram_chat_id, timeout, post), "OK"),
        ("SMS", sms, f"{ACCEPTED}: confirm the text arrived on your phone"),
        ("call", call, f"{ACCEPTED}: your phone should ring now"),
    ]
    results = []
    for name, sink, ok in sinks:
        try:
            sink(alarm)
            results.append((name, ok))
        except ChannelError as exc:
            results.append((name, f"{_FAILED}: {exc}"))
    return results


def fallback_check(channels: ChannelSettings, settings: AlarmSettings, post: Post, out: Any) -> int:
    """The real hub with Telegram made to fail on this machine (nothing is sent to
    Telegram): the test alarm must go by SMS, and "ALARM CHANNEL DOWN: Telegram" by
    SMS and a phone call. Waits for the fallback, then reports what Twilio accepted."""
    accepted: list[str] = []

    def telegram_down(
        url: str, data: Mapping[str, str], auth: tuple[str, str] | None, timeout: float
    ) -> int:
        if url.startswith(_TELEGRAM_URL):
            return int(HTTPStatus.SERVICE_UNAVAILABLE)  # simulated: never sent
        status = post(url, data, auth, timeout)
        if status in HTTPStatus.__members__.values() and HTTPStatus(status).is_success:
            accepted.append("call" if url.endswith("Calls.json") else "SMS")
        return status

    hub = alarm_hub(
        lambda alarm: print(f"recorded: {alarm.message}", file=out),
        settings,
        channels,
        telegram_down,
    )
    hub.submit(Alarm(datetime.now(UTC), "TEST: fallback check (Telegram simulated down)"))
    wait = timedelta(seconds=2 * settings.retry_seconds + 2 * settings.channel_timeout_seconds)
    time.sleep(wait.total_seconds())
    hub.close(timedelta(seconds=settings.channel_timeout_seconds))
    print(f"Twilio accepted: {', '.join(accepted) or 'nothing'}", file=out)
    print(
        "On your phone you should now have: a text with the test alarm, a text "
        "'ALARM CHANNEL DOWN: Telegram', and one call. ACCEPTED is not proof of delivery: "
        "check the phone.",
        file=out,
    )
    return 0 if accepted.count("SMS") >= 2 and "call" in accepted else 1


def main(
    argv: list[str] | None = None,
    out: Any = None,
    post: Post = http_post,
    fetch: Fetch = http_get_json,
) -> int:
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="python -m footprint.live.alarm_setup")
    parser.add_argument("action", choices=["chat-id", "test", "test-fallback"])
    parser.add_argument(
        "--telegram-only", action="store_true", help="test only Telegram (DEGRADED_ALERTING)"
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    env = read_env_file(str(args.env))
    alarm_settings = load_profiles(args.config, "nq.yaml").execution.alarms
    timeout = float(alarm_settings.channel_timeout_seconds)
    if args.action == "chat-id":
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            print("TELEGRAM_BOT_TOKEN is missing in .env", file=out)
            return 2
        try:
            found = chat_ids(token, timeout, fetch)
        except ChannelError as exc:
            print(f"could not ask Telegram: {exc}", file=out)
            return 1
        if not found:
            print(
                "no messages yet: send your bot any message in Telegram, then run again", file=out
            )
            return 1
        for chat_id, name in found:
            print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})", file=out)
        return 0
    if args.action == "test" and args.telegram_only:
        try:
            telegram = TelegramOnlySettings.from_env(env)
        except ValueError as exc:
            print(str(exc), file=out)
            return 2
        sink = TelegramSink(
            telegram.telegram_token, telegram.telegram_chat_id, timeout, post, DEGRADED_BANNER
        )
        try:
            sink(Alarm(datetime.now(UTC), "TEST: Telegram-only alarm check. No action needed"))
            print("Telegram: OK", file=out)
        except ChannelError as exc:
            print(f"Telegram: {_FAILED}: {exc}", file=out)
            return 1
        print(f"{AlertingMode.TELEGRAM_ONLY.value}: this is NOT full alarm coverage", file=out)
        return 0
    try:
        channels = ChannelSettings.from_env(env)
    except ValueError as exc:
        print(str(exc), file=out)
        return 2
    if args.action == "test-fallback":
        return fallback_check(channels, alarm_settings, post, out)
    results = send_tests(channels, timeout, post)
    for name, result in results:
        print(f"{name}: {result}", file=out)
    return 1 if any(result.startswith(_FAILED) for _, result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
