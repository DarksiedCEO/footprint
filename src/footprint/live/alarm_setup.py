"""Set up and check the phone alarms (D20).

    uv run python -m footprint.live.alarm_setup chat-id   # after messaging your bot once
    uv run python -m footprint.live.alarm_setup test      # one test alarm per channel

Reads the keys from .env. Never prints a token, a password or a phone number.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from footprint.brokers.ibkr.binding import read_env_file
from footprint.core.config import load_profiles
from footprint.execution.channels import (
    ChannelError,
    ChannelSettings,
    Post,
    TelegramSink,
    TwilioCallSink,
    TwilioSmsSink,
    http_post,
)
from footprint.execution.management.malik import Alarm

Fetch = Callable[[str, float], Any]
ACCEPTED = "ACCEPTED by Twilio (not proof of delivery)"
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


def main(
    argv: list[str] | None = None,
    out: Any = None,
    post: Post = http_post,
    fetch: Fetch = http_get_json,
) -> int:
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="python -m footprint.live.alarm_setup")
    parser.add_argument("action", choices=["chat-id", "test"])
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    env = read_env_file(str(args.env))
    timeout = float(load_profiles(args.config, "nq.yaml").execution.alarms.channel_timeout_seconds)
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
    try:
        channels = ChannelSettings.from_env(env)
    except ValueError as exc:
        print(str(exc), file=out)
        return 2
    results = send_tests(channels, timeout, post)
    for name, result in results:
        print(f"{name}: {result}", file=out)
    return 1 if any(result.startswith(_FAILED) for _, result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
